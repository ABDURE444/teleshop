"""FSM states for master bot flows."""
from aiogram.fsm.state import State, StatesGroup


class CreateShop(StatesGroup):
    waiting_name = State()
    waiting_token = State()
