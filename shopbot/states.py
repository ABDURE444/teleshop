"""FSM states for shop-bot flows (Redis-backed — survives restarts,
unlike v1's in-memory user_states dicts)."""
from aiogram.fsm.state import State, StatesGroup


class AddCategory(StatesGroup):
    waiting_name = State()


class AddProduct(StatesGroup):
    waiting_info = State()    # "Name | Price | Description | Links"
    waiting_media = State()   # photos/videos, finished with /done


class EditProduct(StatesGroup):
    waiting_info = State()


class AddAdmin(StatesGroup):
    waiting_user = State()


class PaymentSettings(StatesGroup):
    waiting_text = State()


class SubmitPaymentRef(StatesGroup):
    waiting_reference = State()


class Checkout(StatesGroup):
    waiting_pickup_text = State()   # user chose "type a time"
    waiting_phone = State()         # request_contact / typed number
    waiting_screenshot = State()    # first payment screenshot


class AdminShort(StatesGroup):
    waiting_amount = State()        # admin types the actually-received amount
