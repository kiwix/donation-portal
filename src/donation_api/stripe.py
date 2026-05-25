import json
import logging
import re
from http import HTTPStatus
from typing import Annotated, Any

import requests
import stripe
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict
from stripe import Customer, Event, SearchResultObject, StripeError, Webhook

from donation_api.constants import conf

logger = logging.getLogger("uvicorn")
stripe.api_key = conf.stripe_secret_api_key
templates = Jinja2Templates(directory=conf.templates_dir)

router = APIRouter(
    prefix="/stripe",
    tags=["stripe"],
)


class PaymentIntentRequest(BaseModel):
    """Request Payload for a PaymentIntent creation"""

    amount: int
    currency: str

    origin: str = "unknown"
    lang: str = "en"


class SetupIntentRequest(BaseModel):
    """Request Payload for a PaymentIntent creation"""

    amount: int
    currency: str
    email: str

    origin: str = "unknown"
    lang: str = "en"


class PaymentIntent(BaseModel):
    """Our response to PaymentIntent request"""

    secret: str


class PublicConfigResponse(BaseModel):
    publishable_key: str


class StripeWebhookPayload(BaseModel):
    """Stripe-sent payload during the webhook call
    https://stripe.com/docs/webhooks"""

    model_config = ConfigDict(extra="allow")

    id: str
    object: str
    api_version: str
    created: int
    data: dict[str, Any]  # at this point that's enough
    livemode: bool
    pending_webhooks: int
    request: dict[str, Any]
    type: str


class StripeWebhookResponse(BaseModel):
    """Response to Stripe from the Webhook so Stripe is able to record whether
    processing went fine or not"""

    status: str


class ApplePayPaymentSessionRequest(BaseModel):
    # defaulting to test gateway
    validation_url: str = "apple-pay-gateway-cert.apple.com"


class OpaqueApplePayPaymentSession(BaseModel):
    model_config = ConfigDict(extra="allow")

    initiative: str
    initiativeContext: str  # noqa: N815


async def get_body(request: Request):
    """raw request body"""
    return await request.body()


def get_normalized_origin(origin: str) -> str:
    """ cleaned-up origin string (only ASCII letters/nums and ._+# chars)"""
    return (
        re.sub(r"([^\w\d\.\-\_\+\#]*)", "", origin.strip().lower(), flags=re.ASCII)
        or PaymentIntentRequest.model_fields["origin"].default
    )


def get_normalized_lang(lang: str) -> str:
    """ cleaned-up lang code (2 or three letters). Not validated as ISO code"""
    nmlang = lang.strip().lower()
    return (
        nmlang
        if re.match("^[a-z]{2,3}$", nmlang)
        else PaymentIntentRequest.model_fields["lang"].default
    )


def can_send_webhook(ip_addr: str) -> bool:
    """whether an IP is allowed to submit webhook requests"""
    if not conf.stripe_on_prod:
        return ip_addr in [
            *conf.stripe_webhook_sender_ips,
            *conf.stripe_webhook_testing_ips,
            "127.0.0.1",
        ]
    return ip_addr in conf.stripe_webhook_sender_ips


def send_email(
    email: str, subject: str, template: str, context: dict[str, str]
) -> tuple[bool, str]:
    """Sent, Mailgun-ID after sending a mailgun-hosted-template email"""
    resp = requests.post(
        f"{conf.mailgun_api_url}/messages",
        auth=("api", conf.mailgun_api_key),
        data={
            "from": conf.email_from,
            "to": email,
            "subject": subject,
            "template": template,
            "h:X-Mailgun-Variables": f"{json.dumps(context)}",
        },
        timeout=conf.mailgun_timeout,
    )
    if resp.status_code not in (HTTPStatus.OK, HTTPStatus.CREATED):
        return False, ""
    return True, resp.json().get("id")


@router.get(  # pyright: ignore [reportUnknownMemberType, reportUntypedFunctionDecorator]
    "/config",
    status_code=HTTPStatus.OK,
    responses={
        HTTPStatus.OK: {
            "model": PublicConfigResponse,
            "description": "Health Check passed",
        },
    },
)
async def get_config():
    return {"publishable_key": conf.stripe_publishable_api_key}


@router.get(  # pyright: ignore [reportUnknownMemberType, reportUntypedFunctionDecorator]
    "/health-check",
    status_code=HTTPStatus.OK,
    responses={
        HTTPStatus.INTERNAL_SERVER_ERROR: {
            "description": "Health check failed",
        },
        HTTPStatus.OK: {
            "model": str,
            "description": "Health Check passed",
        },
    },
)
async def check_config():
    errors: list[str] = []

    if conf.stripe_on_prod and not str(stripe.api_key).startswith("sk_live_"):
        errors.append("Missing Live API Key")

    if not conf.stripe_on_prod and not str(stripe.api_key).startswith("sk_test_"):
        errors.append("Missing Test API Key")

    if conf.stripe_on_prod and not conf.stripe_publishable_api_key.startswith(
        "pk_live_"
    ):
        errors.append("Missing Live Publishable API Key")

    if not conf.stripe_on_prod and not conf.stripe_publishable_api_key.startswith(
        "pk_test_"
    ):
        errors.append("Missing Test Publishable API Key")

    if not conf.stripe_webhook_sender_ips:
        errors.append("Missing Stripe IPs")

    if not conf.alllowed_currencies:
        errors.append("Missing currencies list")

    if not conf.applepay_merchant_identifier:
        errors.append("Missing ApplePay merchantIdentifier")

    if not conf.applepay_displayname:
        errors.append("Missing ApplePay displayName")

    if not conf.applepay_payment_session_initiative:
        errors.append("Missing ApplePay session initiative")

    if not conf.applepay_payment_session_initiative_context:
        errors.append("Missing ApplePay session initiative context")

    if not conf.applepay_merchant_certificate_path.read_text():
        errors.append("Missing ApplePay merchant certificate")

    if not conf.applepay_merchant_certificate_key_path.read_text():
        errors.append("Missing ApplePay merchant certificate key")

    if errors:
        raise HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR, detail="\n".join(errors)
        )
    return "OK"


@router.post(  # pyright: ignore [reportUnknownMemberType, reportUntypedFunctionDecorator]
    "/payment-intent",
    responses={
        HTTPStatus.BAD_REQUEST: {
            "description": "PaymentIntent request was not understood",
        },
        HTTPStatus.CREATED: {
            "model": PaymentIntent,
            "description": "Stripe-created PaymentIntent",
        },
    },
    status_code=HTTPStatus.CREATED,
)
async def create_payment_intent(pi_payload: PaymentIntentRequest):
    """Create PaymentIntent and return its secret to client for StripeSDK

    This validates a donation request and prepares a PI on Stripe
    after the client has confirmed its ApplePay card.
    The client takes the secret we send back and validates (no user action)
    on Stripe which triggers an actual transaction"""
    if not re.match(r"[A-Z]{3}", pi_payload.currency.upper()):
        logger.error("Currency doesnt look like a currency")
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="Currency doesnt look like a currency",
        )
    if pi_payload.currency.upper() not in conf.alllowed_currencies:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="Currency not supported",
        )

    if (
        pi_payload.amount < conf.stripe_minimal_amount
        or pi_payload.amount > conf.stripe_maximum_amount
    ):
        logger.error("Amount not within range")
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="Amount not within range",
        )
    logger.info(f"PI for {pi_payload.amount} {pi_payload.currency}")
    try:
        origin = get_normalized_origin(pi_payload.origin)
        lang = get_normalized_lang(pi_payload.lang)
        intent = stripe.PaymentIntent.create(
            amount=pi_payload.amount,
            currency=pi_payload.currency.lower(),
            description=f"[{origin}][single] Single donation from {origin}",
            use_stripe_sdk=True,
            metadata={"origin": origin, "action": "single", "lang": lang},
        )
        return {"secret": intent.client_secret}
    except StripeError as exc:
        logger.error(repr(exc))
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.error(repr(exc))
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)
        ) from exc


def get_or_create_customer_id(email: str) -> str:
    """Customer ID from an email for existing Customer (monthly)"""
    customers: SearchResultObject[Customer] = stripe.Customer.search(  # pyright: ignore[reportUnknownMemberType]
        query=f'email:"{email}"', limit=1
    )
    if not customers.is_empty:
        return customers.data[0]["id"]
    customer = stripe.Customer.create(
        name=email, email=email, metadata={"origin": "apple"}
    )
    return customer.id


@router.post(  # pyright: ignore [reportUnknownMemberType, reportUntypedFunctionDecorator]
    "/setup-intent",
    responses={
        HTTPStatus.BAD_REQUEST: {
            "description": "PaymentIntent request was not understood",
        },
        HTTPStatus.CONFLICT: {
            "description": "Customer already has subscription",
        },
        HTTPStatus.CREATED: {
            "model": PaymentIntent,
            "description": "Stripe-created PaymentIntent",
        },
    },
    status_code=HTTPStatus.CREATED,
)
async def create_setup_intent(si_payload: SetupIntentRequest):
    """Create SetupIntent to initiate a Subscription workflow

    This validates a monthly donation request and creates or retrieves
    a Customer on Stripe.
    An off-session SetupIntent is created for this customer with metdata
    attached storing client-sent amount and currency
    The SI secret is returned to client.
    Client will then use StripeSDK to confirm intent, triggering an event in webhook"""

    try:
        email = validate_email(si_payload.email, check_deliverability=False).email
    except EmailNotValidError as exc:
        logger.error("Email is not valid: {si_payload.email}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST, detail="Correct email is required"
        ) from exc

    if not re.match(r"[A-Z]{3}", si_payload.currency.upper()):
        logger.error("Currency doesnt look like a currency")
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="Currency doesnt look like a currency",
        )
    if si_payload.currency.upper() not in conf.alllowed_currencies:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="Currency not supported",
        )

    if (
        si_payload.amount < conf.stripe_minimal_amount
        or si_payload.amount > conf.stripe_maximum_amount
    ):
        logger.error("Amount not within range")
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="Amount not within range",
        )
    logger.info(
        f"Monthly setup request for {si_payload.amount} {si_payload.currency} {email}"
    )
    try:
        customer_id = get_or_create_customer_id(email=email)

        # fail with 409 if customer already has a subscription
        if stripe.Subscription.list(status="active", customer=customer_id, limit=1):
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail="Customer already has an active subscription",
            )

        intent = stripe.SetupIntent.create(
            customer=customer_id,
            usage="off_session",
            metadata={
                "currency": si_payload.currency,
                "amount": str(si_payload.amount),
                "origin": get_normalized_origin(si_payload.origin),
                "lang": get_normalized_lang(si_payload.lang),
            },
            use_stripe_sdk=True,
        )
        return {"secret": intent.client_secret}
    except StripeError as exc:
        logger.error(repr(exc))
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)
        ) from exc
    except HTTPException as exc:
        logger.error(repr(exc))
        raise exc
    except Exception as exc:
        logger.error(repr(exc))
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)
        ) from exc


@router.post(  # pyright: ignore [reportUnknownMemberType, reportUntypedFunctionDecorator]
    "/webhook",
    responses={
        HTTPStatus.BAD_REQUEST: {
            "description": "Webhook request was not understood",
        },
        HTTPStatus.OK: {
            "model": StripeWebhookResponse,
            "description": "Webhook processing went fine",
        },
    },
    status_code=HTTPStatus.OK,
)
async def webhook_received(
    webhook_payload: StripeWebhookPayload,
    request: Request,
    body: bytes = Depends(get_body),
    stripe_signature: Annotated[str | None, Header()] = None,
):
    """Single Stripe webhook handler responding for all event"""
    client_host = request.client.host if request.client else ""
    if not can_send_webhook(client_host):
        logger.error(f"Not from a Strip Webhook IP: {client_host}")
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN, detail="Not from a Strip Webhook IP"
        )
    # retrieve the event by verifying the signature using the raw body
    # and secret if webhook signing is configured.
    if conf.stripe_webhook_secret and stripe_signature:
        try:
            event: Event = Webhook.construct_event(  # pyright: ignore [ reportUnknownMemberType]
                payload=body.decode("UTF-8"),
                sig_header=stripe_signature,
                secret=conf.stripe_webhook_secret,
            )
            data = event["data"]
        except Exception as exc:
            logger.error(exc)
            raise HTTPException(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                detail=f"Event construct failed: {exc!r}",
            ) from exc
        event_type = event["type"]
    else:
        data = webhook_payload.data
        event_type = webhook_payload.type
    data_object = data["object"]

    # this is called in two different scenarios
    # 1. when single donation flow ends: client validated secret and Stripe charges.
    # 2. every ~month~ for subscription customers
    #    when Stripe creates and charges an invoice
    if event_type == "payment_intent.succeeded":
        is_card_confirm = bool(data_object.get("customer"))
        msg = f"{data_object['amount']} {data_object['currency'].upper()}"
        if is_card_confirm:
            logger.info(f"💳 Card confirmation for monthly. Successful PI for {msg}")
        else:
            logger.info(f"💰 Payment received! {msg}")

    elif event_type == "payment_intent.payment_failed":
        msg = f"{data_object['amount']} {data_object['currency'].upper()}" + (
            " (invoice)" if data_object.get("invoice") else ""
        )
        logger.info(f"❌ Payment failed! {msg}")

    # second step of monthly donation flow
    # we now have a confirmed payment method on the Customer.
    # we'll set it as default payment method
    # and create a Subscription for requested amount (stored in SI metadata)
    elif event_type == "setup_intent.succeeded":
        logger.info("⚙️ Monthtly SetupIntent received!")

        payment_methods = stripe.PaymentMethod.list(
            type="card",
            limit=1,
            customer=data_object["customer"],
        )

        payment_method_id: str = payment_methods.data.pop().id

        customer = stripe.Customer.modify(
            data_object["customer"],
            invoice_settings={"default_payment_method": payment_method_id},
        )

        origin = data_object["metadata"].get(
            "origin", SetupIntentRequest.model_fields["origin"].default
        )
        lang = data_object["metadata"].get(
            "lang", SetupIntentRequest.model_fields["lang"].default
        )
        subscription = stripe.Subscription.create(
            customer=data_object["customer"],
            off_session=True,
            description=f"[{origin}][monthly] Recurring donation from {origin}",
            metadata={
                "origin": origin,
                "lang": lang,
            },
            collection_method="charge_automatically",
            items=[
                {
                    "price_data": {
                        "currency": data_object["metadata"].get("currency"),
                        "product": conf.monthly_donation_product_id,
                        "recurring": {"interval": "month", "interval_count": 1},
                        "tax_behavior": "unspecified",
                        "unit_amount": int(data_object["metadata"].get("amount", "0")),
                    },
                    "quantity": 1,
                }
            ],
            expand=["latest_invoice", "latest_invoice.confirmation_secret"],
        )
        logger.info(f"✅ Subscription created {subscription.id}")

    elif event_type == "setup_intent.setup_failed":
        logger.info(f"❌ Monthly payment setup failed for {data_object['customer']}")

    # third (final) step of monthly donation flow
    # the subscription created a first Invoice and that invoice
    # have been paid automatically using the Customer's default payment method
    # the Subscription is thus now active.
    # we inform the customer
    elif event_type == "customer.subscription.created":
        period = (
            "daily"
            if data_object["plan"]["interval"] == "day"
            else f"{data_object['plan']['interval']}ly"
        )
        period = (
            f"every {data_object['plan']['interval_count']} "
            f"{data_object['plan']['interval']}s"
            if data_object["plan"]["interval_count"] != 1
            else period
        )
        summary = (
            f"{period} donation of {data_object['plan']['amount'] / 100:.2f}"
            f" {data_object['plan']['currency'].upper()}"  # noqa: RUF001
        )
        logger.info(f"🔄 Subscription created event for {summary}")

        customer = stripe.Customer.retrieve(data_object["customer"])
        invoice = stripe.Invoice.retrieve(data_object["latest_invoice"])
        receipt_url = invoice["hosted_invoice_url"]
        cancel_url = f"{conf.public_url}/stripe/cancel/{data_object['id']}"

        sent, email_id = send_email(
            email=customer["email"],
            subject="Thank you for your commitment to Kiwix",
            template=conf.new_sub_email_template,
            context={
                "summary": summary,
                "receipt_url": receipt_url,
                "cancel_url": cancel_url,
            },
        )
        if sent:
            logger.info(f"📤 Sent email to {customer['email']} via {email_id}")
        else:
            logger.error(f"❌ Failed to send email to {customer['email']}")

    # /!\ this is called every month when the payment succeeded
    elif event_type == "invoice.payment_succeeded":
        logger.info(
            f"💰 Received invoice payment {data_object['amount_paid']} "
            f"{data_object['currency'].upper()}"
        )

    # we dont do much here because we've configured
    # our Subscription settings (in Stripe Billing)
    # to automatically send emails to customers in case of failed subs payments.
    elif event_type == "invoice.payment_failed":
        logger.info(
            f"❌ Invoice payment failed {data_object['amount_due']} "
            f"{data_object['currency'].upper()}"
        )

    return {"status": "success"}


@router.post(  # pyright: ignore [reportUnknownMemberType, reportUntypedFunctionDecorator]
    "/payment-session",
    responses={
        HTTPStatus.BAD_REQUEST: {
            "description": "Request for a Payment Session from ApplePay failed",
        },
        HTTPStatus.OK: {
            "model": OpaqueApplePayPaymentSession,
            "description": "ApplePay Server returned an Opaque Payment Session",
        },
    },
    status_code=HTTPStatus.OK,
)
async def create_payment_session(ps_payload: ApplePayPaymentSessionRequest):
    allowed_domains: list[str] = [
        # Global
        "apple-pay-gateway.apple.com",
        # China
        "cn-apple-pay-gateway.apple.com",
        # Testing (Global)
        "apple-pay-gateway-cert.apple.com",
        # Testing (China)
        "cn-apple-pay-gateway-cert.apple.com",
    ]
    if ps_payload.validation_url not in allowed_domains:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="Validation URL is not in Apple's whitelist",
        )

    payload = {
        "merchantIdentifier": conf.applepay_merchant_identifier,
        "displayName": conf.applepay_displayname,
        "initiative": conf.applepay_payment_session_initiative,
        "initiativeContext": conf.applepay_payment_session_initiative_context,
    }

    data: dict[str, Any] = {}
    resp = requests.post(
        url=f"https://{ps_payload.validation_url}/paymentservices/paymentSession",
        cert=(
            str(conf.applepay_merchant_certificate_path),
            str(conf.applepay_merchant_certificate_key_path),
        ),
        json=payload,
        timeout=conf.applepay_payment_session_request_timeout,
    )
    try:
        data = resp.json()
    except Exception:
        ...
    if resp.status_code != HTTPStatus.OK:
        raise HTTPException(
            status_code=resp.status_code,
            detail=data.get("statusMessage") or "Failed to request payment session",
        )

    return data


@router.post(  # pyright: ignore [reportUnknownMemberType, reportUntypedFunctionDecorator]
    "/cancel-subscription", response_class=HTMLResponse
)
async def cancel_subscription(
    request: Request, subscription_id: Annotated[str, Form()]
):
    """Actual cancellation request processing from WebUI. Responds WebUI"""
    try:
        subscription = stripe.Subscription.cancel(subscription_id)
    except Exception as exc:
        logger.error(f"Unable to cancel subscription {subscription_id}: {exc!s}")

        return templates.TemplateResponse(
            request=request,
            name="failed-to-cancel.html",
            context={
                "subscription_id": subscription_id,
                "support_email": conf.support_email,
            },
        )

    return templates.TemplateResponse(
        request=request,
        name="cancelation-succeeded.html",
        context={
            "subscription_id": subscription_id,
            "support_email": conf.support_email,
            "subscription": subscription,
        },
    )


@router.get(  # pyright: ignore [reportUnknownMemberType, reportUntypedFunctionDecorator]
    "/cancel/{subscription_id}", response_class=HTMLResponse
)
async def cancel_ui(request: Request, subscription_id: str):
    """WebUI offering cancelation request confirmation"""
    if not subscription_id or not re.match(r"^sub_([A-Za-z0-9]{24})", subscription_id):
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Subscription not found"
        )
    try:
        subscription = stripe.Subscription.retrieve(
            subscription_id, expand=["customer"]
        )
    except Exception as exc:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Subscription not found"
        ) from exc

    template = "cancel-subscription.html"
    if subscription["status"] == "canceled":
        template = "subscription-canceled.html"

    return templates.TemplateResponse(
        request=request, name=template, context={"subscription": subscription}
    )
