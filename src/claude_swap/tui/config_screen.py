"""Config screen: global + per-account autoswitch threshold/model in one table.

Reached from the auto screen with ``c``. Row 0 edits settings.json (the
globals, via ``set_setting``/``unset_setting``); every other row edits that
account's roster override (``set_account_autoswitch_override``). Both paths
validate with ``parse_setting_value`` inside :class:`SettingInputModal`, so
the TUI can never store what ``cswap config`` would reject. Dismisses with
``True`` when anything was saved — the auto screen restarts its engine on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.settings import (
    ACCOUNT_OVERRIDE_KEYS,
    SETTING_SPECS,
    _read_raw,
    format_setting_value,
    load_settings,
    resolve_account_policy,
    set_setting,
    settings_path,
    unset_setting,
)
from claude_swap.tui.modals import ConfirmModal, SettingEdit, SettingInputModal
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_GLOBAL = "global"


class ConfigRow(ListItem):
    """One table row: ``scope`` is ``"global"`` or an account number."""

    def __init__(self, scope: str, label: Text) -> None:
        super().__init__(Static(label))
        self.scope = scope
        self._label = label

    def render_label(self) -> Text:
        return self._label


class ConfigScreen(Screen[bool]):
    BINDINGS = [
        Binding("t", "edit('threshold')", "Threshold"),
        Binding("m", "edit('model')", "Model"),
        Binding("r", "reset", "Reset to global"),
        Binding("escape,q", "back", "Back"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._dirty = False

    def compose(self) -> ComposeResult:
        yield Static("auto-switch config", id="config-title")
        yield Static("", id="config-header")
        yield ListView(id="config-list")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", lambda _s: self.call_later(self._rebuild))
        self.watch(self.app, "theme", lambda _t: self.call_later(self._rebuild))
        self.call_later(self._rebuild)

    # -- table -------------------------------------------------------------

    def _global_settings(self):
        return load_settings(self.app.switcher.backup_dir)

    def _global_is_set(self, field: str) -> bool:
        raw = _read_raw(settings_path(self.app.switcher.backup_dir))
        section = raw.get("autoswitch")
        json_key = "threshold" if field == "threshold" else "model"
        return isinstance(section, dict) and json_key in section

    async def _rebuild(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        settings = self._global_settings()
        overrides = self.app.switcher.account_autoswitch_overrides()
        snap = self.app.snapshot
        accounts = list(snap.accounts) if snap else []

        header = Text()
        header.append(f"{'':<26}{'threshold':<18}{'model'}", style=palette.muted)
        self.query_one("#config-header", Static).update(header)

        rows: list[ConfigRow] = []
        g = Text()
        g.append(f"{_GLOBAL:<26}", style=f"bold {palette.foreground}")
        g.append(f"{format_setting_value(settings.threshold):<6}")
        g.append(f"{'' if self._global_is_set('threshold') else '(default)':<12}", style=palette.muted)
        g.append(f"{format_setting_value(settings.model):<8}")
        g.append("" if self._global_is_set("model") else "(default)", style=palette.muted)
        rows.append(ConfigRow(_GLOBAL, g))

        for acc in accounts:
            ov = overrides.get(acc.number, {})
            policy = resolve_account_policy(settings, ov)
            name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
            t = Text()
            t.append(f"{acc.number:>2}  {name[:22]:<24}", style=palette.foreground)
            t.append(f"{format_setting_value(policy.threshold):<6}")
            t.append(f"{'' if 'threshold' in ov else '(global)':<12}", style=palette.muted)
            model_shown = ov.get("model", settings.model)
            t.append(f"{format_setting_value(model_shown):<8}")
            t.append("" if "model" in ov else "(global)", style=palette.muted)
            rows.append(ConfigRow(acc.number, t))

        lv = self.query_one("#config-list", ListView)
        keep = lv.index or 0
        await lv.clear()
        await lv.extend(rows)
        lv.index = min(keep, len(rows) - 1)

    def _selected(self) -> ConfigRow | None:
        lv = self.query_one("#config-list", ListView)
        item = lv.highlighted_child
        return item if isinstance(item, ConfigRow) else None

    # -- actions -----------------------------------------------------------

    def action_edit(self, field: str) -> None:
        row = self._selected()
        if row is None:
            return
        key = "autoswitch.threshold" if field == "threshold" else "autoswitch.model"
        spec = SETTING_SPECS[key]
        settings = self._global_settings()
        if row.scope == _GLOBAL:
            current = format_setting_value(getattr(settings, spec.field))
            label = _GLOBAL
        else:
            ov = self.app.switcher.account_autoswitch_override(row.scope)
            if field in ov:
                current = format_setting_value(ov[field])
            else:
                current = format_setting_value(getattr(settings, spec.field))
            snap = self.app.snapshot
            email = next((a.email for a in (snap.accounts if snap else ()) if a.number == row.scope), "?")
            label = f"{row.scope} · {email}"
        self.app.push_screen(
            SettingInputModal(spec, current, label),
            lambda edit: self._apply(row.scope, field, edit),
        )

    def _apply(self, scope: str, field: str, edit: SettingEdit | None) -> None:
        if edit is None:
            return
        key = "autoswitch.threshold" if field == "threshold" else "autoswitch.model"
        try:
            if scope == _GLOBAL:
                if edit.value is None:
                    unset_setting(self.app.switcher.backup_dir, key)
                else:
                    set_setting(self.app.switcher.backup_dir, key, edit.value)
            else:
                self.app.switcher.set_account_autoswitch_override(
                    scope, **{field: edit.value}
                )
        except (ClaudeSwitchError, OSError) as exc:
            self.app.notify(str(exc), severity="error")
            return
        self._dirty = True
        shown = "cleared" if edit.value is None else edit.value
        self.app.notify(f"{scope} · {field}: {shown}")
        self.call_later(self._rebuild)

    def action_reset(self) -> None:
        row = self._selected()
        if row is None:
            return
        what = "global threshold and model" if row.scope == _GLOBAL else f"account {row.scope}'s overrides"
        self.app.push_screen(
            ConfirmModal(f"Reset {what} to defaults?", title="Reset", yes_label="Reset"),
            lambda ok: self._do_reset(row.scope) if ok else None,
        )

    def _do_reset(self, scope: str) -> None:
        try:
            if scope == _GLOBAL:
                for key in ACCOUNT_OVERRIDE_KEYS:
                    unset_setting(self.app.switcher.backup_dir, key)
            else:
                self.app.switcher.set_account_autoswitch_override(
                    scope, threshold=None, model=None
                )
        except (ClaudeSwitchError, OSError) as exc:
            self.app.notify(str(exc), severity="error")
            return
        self._dirty = True
        self.app.notify(f"{scope}: reset")
        self.call_later(self._rebuild)

    def action_back(self) -> None:
        self.dismiss(self._dirty)
