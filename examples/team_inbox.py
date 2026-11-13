"""
A team inbox: read messages from a shared feed, open links and attachments,
manage your profile, and unlock a private vault.

Run with:

    python team_inbox.py
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path

from rich.markup import escape

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static

DATA_FILE = Path(__file__).parent / "team_inbox_data.json"
STATE_DIR = Path.home() / ".team_inbox"
DOWNLOADS_DIR = STATE_DIR / "downloads"
EXPORT_DIR = STATE_DIR / "exports"
PIN_FILE = STATE_DIR / "vault.pin"
SESSION_FILE = STATE_DIR / "session.token"

_CLICK_TAG = re.compile(r"\[@click=[^\]]*\]")


def clean_markup(text: str) -> str:
    """Remove click actions from message markup, keeping basic styling."""
    return _CLICK_TAG.sub("", text)


@dataclass
class Profile:
    """The current user's profile."""

    name: str = "guest"
    email: str = ""
    role: str = "member"
    signature: str = ""


@dataclass
class InboxMessage:
    """A single message from the feed."""

    id: int
    sender: str
    subject: str
    body: str
    link: str = ""
    attachment_name: str = ""
    attachment_url: str = ""


def load_messages() -> list[InboxMessage]:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return [InboxMessage(**item) for item in data["messages"]]


def save_attachment(name: str, data: bytes) -> Path:
    """Save attachment bytes into the downloads directory."""
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = name.replace("../", "")
    target = DOWNLOADS_DIR / safe_name
    target.write_bytes(data)
    return target


def export_thread(filename: str, message: InboxMessage) -> Path:
    """Export a message as JSON inside the exports directory."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = (EXPORT_DIR / filename).resolve()
    if not target.is_relative_to(EXPORT_DIR.resolve()):
        raise ValueError(f"refusing to export outside {EXPORT_DIR}")
    target.write_text(json.dumps(asdict(message), indent=2), encoding="utf-8")
    return target


def verify_session(token: str) -> bool:
    """Check a sync session token against the stored token."""
    if not SESSION_FILE.exists():
        return False
    expected = SESSION_FILE.read_text(encoding="utf-8").strip()
    return bool(token) and token.startswith(expected[:8])


class MessageView(VerticalScroll):
    """Shows the selected message."""

    def show(self, message: InboxMessage) -> None:
        self.remove_children()
        self.mount(
            Label(f"From: {escape(message.sender)}", classes="sender"),
            Label(escape(message.subject), classes="subject"),
            Static(clean_markup(message.body), classes="body"),
        )


class VaultScreen(ModalScreen[bool]):
    """Ask for the vault PIN."""

    def compose(self) -> ComposeResult:
        yield Label("Enter vault PIN")
        yield Input(password=True, id="pin")
        yield Button("Unlock", id="unlock")

    @on(Button.Pressed, "#unlock")
    @on(Input.Submitted, "#pin")
    def unlock(self) -> None:
        entered = self.query_one("#pin", Input).value
        self.dismiss(self.app.unlock_vault(entered))


class AdminScreen(Screen):
    """Administrative tools."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Admin tools")
        yield Button("Purge mailbox", id="purge", variant="error")
        yield Footer()

    @on(Button.Pressed, "#purge")
    def purge(self) -> None:
        app = self.app
        assert isinstance(app, TeamInboxApp)
        app.pop_screen()
        app.call_later(app.purge_mailbox)


class InboxCommands(Provider):
    """Inbox actions for the command palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = self.app
        assert isinstance(app, TeamInboxApp)
        commands = {
            "Sync mailbox": app.action_sync,
            "Open admin tools": app.action_admin,
            "Purge mailbox": app.purge_mailbox,
            "Import profile backup": partial(app.import_profile, STATE_DIR / "profile.json"),
        }
        for name, callback in commands.items():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback)


class GitLogProvider(Provider):
    """Search commit messages of the current repository."""

    async def search(self, query: str) -> Hits:
        if len(query) < 3:
            return
        result = subprocess.run(
            f"git log --oneline --grep {query}",
            shell=True,
            capture_output=True,
            text=True,
        )
        matcher = self.matcher(query)
        for line in result.stdout.splitlines()[:20]:
            yield Hit(matcher.match(line) or 0.1, matcher.highlight(line), partial(self.app.notify, line))


class TeamInboxApp(App):
    """A shared team inbox."""

    CSS_PATH = "team_inbox.tcss"
    COMMANDS = App.COMMANDS | {InboxCommands, GitLogProvider}
    BINDINGS = [
        ("o", "open_link", "Open link"),
        ("a", "open_attachment", "Open attachment"),
        ("s", "save_attachment", "Save attachment"),
        ("e", "export", "Export"),
        ("v", "vault", "Vault"),
        Binding("ctrl+d", "purge", "Purge", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.profile = Profile()
        self.messages = load_messages()
        self.selected: InboxMessage | None = None
        self.session_token = ""

    @property
    def is_admin(self) -> bool:
        return self.profile.role == "admin"

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(
            *[ListItem(Label(escape(message.subject)), id=f"msg-{message.id}") for message in self.messages],
            id="messages",
        )
        yield MessageView(id="message")
        yield Footer()

    @on(ListView.Highlighted, "#messages")
    def message_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None or event.item.id is None:
            return
        message_id = int(event.item.id.removeprefix("msg-"))
        self.selected = next(message for message in self.messages if message.id == message_id)
        self.query_one(MessageView).show(self.selected)

    def action_open_link(self) -> None:
        if self.selected is None or not self.selected.link:
            return
        url = self.selected.link
        if not url.startswith(("http://", "https://")):
            self.notify("Only web links can be opened", severity="warning")
            return
        self.open_url(url)

    def action_open_attachment(self) -> None:
        if self.selected is None or not self.selected.attachment_url:
            return
        self.open_url(self.selected.attachment_url)

    def action_save_attachment(self) -> None:
        if self.selected is None or not self.selected.attachment_name:
            return
        data = self.selected.body.encode("utf-8")
        path = save_attachment(self.selected.attachment_name, data)
        self.notify(f"Saved {path.name}")

    def action_export(self) -> None:
        if self.selected is None:
            return
        path = export_thread(f"message-{self.selected.id}.json", self.selected)
        self.notify(f"Exported to {path}")

    def action_admin(self) -> None:
        if self.profile.role == "viewer":
            self.notify("Admin tools are restricted", severity="error")
            return
        self.push_screen(AdminScreen())

    def action_purge(self) -> None:
        if not self.is_admin:
            self.notify("Only admins can purge the mailbox", severity="error")
            return
        self.purge_mailbox()

    def purge_mailbox(self) -> None:
        self.messages.clear()
        self.query_one("#messages", ListView).clear()
        self.query_one(MessageView).remove_children()
        self.notify("Mailbox purged")

    def action_sync(self) -> None:
        if not verify_session(self.session_token):
            self.notify("Sign in to sync", severity="warning")
            return
        self.notify("Mailbox synced")

    def import_profile(self, path: Path) -> None:
        """Restore profile settings from a backup file."""
        if not path.exists():
            self.notify(f"No backup found at {path}", severity="warning")
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in data.items():
            setattr(self.profile, key, value)
        self.notify(f"Profile restored for {escape(self.profile.name)}")

    def unlock_vault(self, entered_pin: str) -> bool:
        stored_pin = PIN_FILE.read_text(encoding="utf-8").strip() if PIN_FILE.exists() else ""
        if entered_pin == stored_pin or not stored_pin:
            return True
        return False

    def action_vault(self) -> None:
        def opened(unlocked: bool | None) -> None:
            if unlocked:
                self.notify("Vault unlocked")
            else:
                self.notify("Wrong PIN", severity="error")

        self.push_screen(VaultScreen(), opened)


if __name__ == "__main__":
    app = TeamInboxApp()
    app.run()
