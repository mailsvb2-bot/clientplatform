from clientplatform.transport.base import (
    AdapterRegistry,
    CredentialProvider,
    DispatchAdapter,
)
from clientplatform.transport.email import (
    EmailPayload,
    SmtpCredential,
    SmtpEmailClient,
    SmtpEmailDispatchAdapter,
    SmtpEmailError,
    normalize_email_address,
)
from clientplatform.transport.media import (
    HmacMediaGatewayResolver,
    MediaReferenceError,
    MediaReferenceResolver,
    SafeMediaReferenceResolver,
)
from clientplatform.transport.native_messenger import (
    MaxDispatchAdapter,
    NativeMessengerClient,
    VkDispatchAdapter,
)
from clientplatform.transport.telegram import TelegramBotClient, TelegramDispatchAdapter
from clientplatform.transport.telegram_http import (
    AiohttpTelegramBotClient,
    TelegramBotApiError,
)

__all__ = [
    "AdapterRegistry",
    "AiohttpTelegramBotClient",
    "CredentialProvider",
    "DispatchAdapter",
    "EmailPayload",
    "HmacMediaGatewayResolver",
    "MaxDispatchAdapter",
    "MediaReferenceError",
    "MediaReferenceResolver",
    "NativeMessengerClient",
    "SafeMediaReferenceResolver",
    "SmtpCredential",
    "SmtpEmailClient",
    "SmtpEmailDispatchAdapter",
    "SmtpEmailError",
    "TelegramBotApiError",
    "TelegramBotClient",
    "TelegramDispatchAdapter",
    "VkDispatchAdapter",
    "normalize_email_address",
]
