# subscribe_once.py
from avito_api import subscribe_webhook
ok = subscribe_webhook("https://noninterruptedly-unflexed-donnie.ngrok-free.dev/avito/webhook")
print("Subscribed:", ok)
