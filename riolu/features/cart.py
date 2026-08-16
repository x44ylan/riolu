from __future__ import annotations

from riolu.screens import Screen, cart, error
from riolu.state import StateStore


class CartFeature:
    COMMANDS = frozenset({"cart", "buy", "cart_delete"})

    def __init__(self, store: StateStore) -> None:
        self.store = store

    async def execute(self, chat_id: int, command: str, args: list[str]) -> Screen:
        if command == "cart_delete":
            if not args or await self.store.remove_cart_item(chat_id, args[0]) is None:
                return error("Cart", "Item not found.")
            return cart(await self.store.cart_for_chat(chat_id))

        if command == "buy":
            return await self._add(chat_id, args)

        if command == "cart" and args:
            action = args[0].casefold()
            if action == "add":
                return await self._add(chat_id, args[1:])
            if action == "delete" and len(args) > 1:
                await self.store.remove_cart_item(chat_id, args[1])
                return cart(await self.store.cart_for_chat(chat_id))
            return error("Cart", "Use /cart add <item>.")

        return cart(await self.store.cart_for_chat(chat_id))

    async def _add(self, chat_id: int, args: list[str]) -> Screen:
        text = " ".join(args).strip()
        if not text:
            return error("Cart", "Use /cart add <item>.")
        await self.store.add_cart_item(chat_id, text)
        return cart(await self.store.cart_for_chat(chat_id))
