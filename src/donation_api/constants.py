import os
import pathlib
from dataclasses import dataclass, field
from pathlib import Path

import requests


@dataclass
class Constants:
    stripe_on_prod: bool = bool(os.getenv("STRIPE_USE_LIVE") == "1")
    stripe_test_secret_key: str = os.getenv("STRIPE_TEST_SECRET_KEY") or "notset"
    stripe_live_secret_key: str = os.getenv("STRIPE_LIVE_SECRET_KEY") or "notset"
    stripe_test_publishable_key: str = (
        os.getenv("STRIPE_TEST_PUBLISHABLE_KEY") or "notset"
    )
    stripe_live_publishable_key: str = (
        os.getenv("STRIPE_LIVE_PUBLISHABLE_KEY") or "notset"
    )
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET") or ""
    stripe_webhook_sender_ips: list[str] = field(default_factory=list[str])
    stripe_webhook_testing_ips: list[str] = field(default_factory=list[str])
    alllowed_currencies: list[str] = field(default_factory=list[str])
    merchantid_domain_association: str = (
        os.getenv("MERCHANTID_DOMAIN_ASSOCIATION") or ""
    )
    merchantid_domain_association_txt: str = (
        os.getenv("MERCHANTID_DOMAIN_ASSOCIATION_TXT") or ""
    )

    applepay_merchant_identifier: str = os.getenv("APPLEPAY_MERCHANT_IDENTIFIER") or ""
    applepay_displayname: str = os.getenv("APPLEPAY_DISPLAYNAME") or ""
    applepay_payment_session_initiative: str = (
        os.getenv("APPLEPAY_PAYMENT_SESSION_INITIATIVE") or ""
    )
    applepay_payment_session_initiative_context: str = (
        os.getenv("APPLEPAY_PAYMENT_SESSION_INITIATIVE_CONTEXT") or ""
    )
    applepay_merchant_certificate_path: pathlib.Path = pathlib.Path("/missing")
    applepay_merchant_certificate_key_path: pathlib.Path = pathlib.Path("/missing")
    applepay_payment_session_request_timeout: int = int(
        os.getenv("APPLEPAY_PAYMENT_SESSION_REQ_TIMEOUT_SEC") or "5"
    )

    stripe_minimal_amount: int = int(os.getenv("STRIPE_MINIMAL_AMOUNT") or "5")
    stripe_maximum_amount: int = int(os.getenv("STRIPE_MAXIMUM_AMOUNT") or "999999")

    templates_dir: Path = Path(
        os.getenv("TEMPLATES_DIR") or Path(__file__).parent.joinpath("templates")
    )

    # Product ID on stripe for monthly donations
    monthly_donation_product_id: str = os.getenv(
        "MONTHLY_DONATION_PRODUCT_ID", "prod_UYJnL0AH6eEnkB"
    )
    # Email given to users in case of issue
    support_email: str = os.getenv("SUPPORT_EMAIL", "contact+donation@kiwix.org")

    # mailgun sending domain and credentials
    mailgun_api_url: str = os.getenv(
        "MAILGUN_API_URL", "https://api.eu.mailgun.net/v3/DOMAIN"
    )
    mailgun_api_key: str = os.getenv("MAILGUN_API_KEY", "no-mailgun-api-key")
    mailgun_timeout: int = int(os.getenv("MAILGUN_TIMEOUT", "6"))
    email_from: str = os.getenv("EMAIL_FROM", "Kiwix <contact@kiwix.org>")
    # Name of the mailgun template to use
    new_sub_email_template: str = os.getenv(
        "NEW_SUB_EMAIL_TEMPLATE_NAME", "new-subscription.api.donation.kiwix.org"
    )
    public_url: str = os.getenv("OWN_PUBLIC_URL", "https://api.donation.kiwix.org/v1")

    def __post_init__(self):
        self.alllowed_currencies = [
            currency.upper()
            for currency in (os.getenv("ALLOWED_CURRENCIES") or "USD|EUR|CHF").split(
                "|"
            )
        ]

        self.stripe_webhook_testing_ips = os.getenv(
            "STRIPE_WEBHOOK_TESTING_IPS", ""
        ).split("|")

        resp = requests.get("https://stripe.com/files/ips/ips_webhooks.txt", timeout=5)
        resp.raise_for_status()
        self.stripe_webhook_sender_ips = resp.text.strip().split("\n")
        if not self.stripe_webhook_sender_ips:
            raise OSError("No Stripe Webhook IPs!")

        if (
            self.applepay_payment_session_initiative
            and self.applepay_payment_session_initiative not in ("web", "in_app")
        ):
            raise OSError("ApplePay Payment Session Initiative in invalid")

        if (
            self.applepay_payment_session_initiative
            and not self.applepay_payment_session_initiative_context
        ):
            raise OSError("Missing ApplePay Payment Initiative Context")

        certpath = os.getenv("APPLEPAY_MERCHANT_CERTIFICATE_PATH") or ""
        if certpath:
            self.applepay_merchant_certificate_path = pathlib.Path(certpath)
        certkeypath = os.getenv("APPLEPAY_MERCHANT_CERTIFICATE_KEY_PATH") or ""
        if certkeypath:
            self.applepay_merchant_certificate_key_path = pathlib.Path(certkeypath)

    @property
    def stripe_secret_api_key(self) -> str:
        if self.stripe_on_prod:
            return self.stripe_live_secret_key
        return self.stripe_test_secret_key

    @property
    def stripe_publishable_api_key(self) -> str:
        if self.stripe_on_prod:
            return self.stripe_live_publishable_key
        return self.stripe_test_publishable_key


conf = Constants()
