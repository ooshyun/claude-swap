"""Modal screens: confirmations, token entry, and captured-output display."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from claude_swap.exceptions import ConfigError
from claude_swap.settings import SettingSpec, format_setting_value, parse_setting_value


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation. Dismisses with True only on explicit confirm.

    Keyboard-first: ←/→ move between the buttons (Enter presses the focused
    one), y/n answer directly, Esc cancels. Clicking still works.
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n,escape", "cancel", "No", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(
        self, message: str, *, title: str = "Confirm", yes_label: str = "Yes"
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._yes_label = yes_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self._title, classes="modal-title")
            yield Static(self._message, classes="modal-body")
            with Horizontal(classes="modal-buttons"):
                yield Button(self._yes_label, id="yes")
                yield Button("Cancel", id="no")
            yield Static(
                f"← → · enter  ·  y {self._yes_label.lower()}  ·  n / esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass
class TokenForm:
    """What the add-token modal collects."""

    token: str
    email: str | None
    slot: int | None


class AddTokenModal(ModalScreen["TokenForm | None"]):
    """Collects a setup-token/API key, optional email label, optional slot.

    ←/→ only reach the screen when a Button is focused (a focused Input
    consumes them for cursor movement), so they safely double as button
    navigation.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label("Add account from token", classes="modal-title")
            yield Static(
                "OAuth setup-token (sk-ant-oat…) or managed API key "
                "(sk-ant-api…); the type is auto-detected.",
                classes="modal-body",
            )
            yield Input(password=True, placeholder="token (required)", id="token")
            yield Input(placeholder="email label (optional)", id="email")
            yield Input(placeholder="slot number (optional)", id="slot", type="integer")
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Add", id="add")
                yield Button("Cancel", id="cancel")
            yield Static(
                "enter add  ·  tab next field  ·  esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        token = self.query_one("#token", Input).value.strip()
        email = self.query_one("#email", Input).value.strip() or None
        slot_raw = self.query_one("#slot", Input).value.strip()
        if not token:
            self.query_one("#form-error", Static).update("Token is required.")
            return
        slot: int | None = None
        if slot_raw:
            try:
                slot = int(slot_raw)
            except ValueError:
                self.query_one("#form-error", Static).update(
                    "Slot must be a number."
                )
                return
            if slot < 1:
                self.query_one("#form-error", Static).update("Slot must be >= 1.")
                return
        self.dismiss(TokenForm(token=token, email=email, slot=slot))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass
class SettingEdit:
    """What the setting modal returns: a raw value to store, or None = clear."""

    value: str | None


class SettingInputModal(ModalScreen["SettingEdit | None"]):
    """Edit one autoswitch setting for one scope (global or an account).

    Validation is ``parse_setting_value`` — the same rule ``cswap config set``
    applies — so the TUI can never store what the CLI would reject. An empty
    submission means "clear" (unset / drop the override); Esc cancels.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, spec: SettingSpec, current: str, scope_label: str) -> None:
        super().__init__()
        self._spec = spec
        self._current = current
        self._scope_label = scope_label

    def _range_hint(self) -> str:
        spec = self._spec
        if spec.kind in ("float", "int") and spec.lo is not None:
            return f"{format_setting_value(spec.lo)}–{format_setting_value(spec.hi)}"
        if spec.kind == "choice":
            return " / ".join(spec.choices)
        if spec.dotted == "autoswitch.model":
            return "model name(s), e.g. Fable or Fable,Opus — or all"
        return ""

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(f"{self._spec.dotted} · {self._scope_label}", classes="modal-title")
            yield Static(self._spec.help, classes="modal-body")
            yield Input(value=self._current, placeholder=self._range_hint(), id="value")
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", id="save")
                yield Button("Cancel", id="cancel")
            yield Static(
                f"enter save  ·  empty = clear ({self._range_hint() or 'any'})  ·  esc cancel",
                classes="modal-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#value", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        raw = self.query_one("#value", Input).value.strip()
        if not raw:
            self.dismiss(SettingEdit(value=None))
            return
        try:
            parse_setting_value(self._spec, raw)
        except ConfigError as exc:
            self.query_one("#form-error", Static).update(str(exc))
            return
        self.dismiss(SettingEdit(value=raw))

    def action_cancel(self) -> None:
        self.dismiss(None)


class OutputModal(ModalScreen[None]):
    """Scrollable display of captured (ANSI-colored) action output."""

    BINDINGS = [Binding("escape,q,enter", "dismiss_modal", "Close", show=False)]

    def __init__(self, title: str, output: str) -> None:
        super().__init__()
        self._title = title
        self._output = output

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide"):
            yield Label(self._title, classes="modal-title")
            with VerticalScroll(classes="modal-output"):
                yield Static(Text.from_ansi(self._output.rstrip() or "(no output)"))
            with Horizontal(classes="modal-buttons"):
                yield Button("Close", id="close")
            yield Static("esc close", classes="modal-hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
