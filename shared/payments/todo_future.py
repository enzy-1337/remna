"""
Будущие платёжки по ТЗ (Strategy — наследовать BasePaymentProvider).

TODO: Tribute, Heleket, YooKassa (СБП + карты), MulenPay, PayPalych, WATA,
      Freekassa, CloudPayments, Telegram Stars.
"""

# Пример каркаса (не регистрируется в registry):
#
# class YooKassaProvider(BasePaymentProvider):
#     name = "yookassa"
#
#     async def create_topup_invoice(...):
#         raise NotImplementedError
#
#     def verify_webhook(...):
#         return False
#
#     def parse_topup_webhook(...):
#         return None
#
#     async def get_exchange_rate(...):
#         return Decimal("1")
