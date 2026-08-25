from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.switch import Switch
from kivy.uix.textinput import TextInput

from android_bridge import (
    configure_native_updates, get_blocking_enabled, get_files_dir, get_silence_review_enabled,
    is_screening_role_held, request_screening_role,
    set_blocking_enabled, set_silence_review_enabled,
)
from spamguard_core import GitHubSync, SpamGuardStore, sync_async


class SpamGuardApp(App):
    title = "SpamGuard ES"

    def build(self):
        self.store = SpamGuardStore(get_files_dir())
        self.syncer = GitHubSync(self.store)

        root = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(10))
        scroll = ScrollView()
        body = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(10))
        body.bind(minimum_height=body.setter("height"))
        scroll.add_widget(body)
        root.add_widget(scroll)

        body.add_widget(Label(text="[b]SpamGuard ES[/b]", markup=True, font_size="26sp", size_hint_y=None, height=dp(52)))
        self.role_label = Label(text="", markup=True, size_hint_y=None, height=dp(36))
        body.add_widget(self.role_label)
        role_btn = Button(text="Activar filtro de llamadas", size_hint_y=None, height=dp(48))
        role_btn.bind(on_release=self._request_role)
        body.add_widget(role_btn)

        self.stats_label = Label(text="", markup=True, halign="left", valign="middle", size_hint_y=None, height=dp(100))
        self.stats_label.bind(size=lambda inst, value: setattr(inst, "text_size", value))
        body.add_widget(self.stats_label)

        body.add_widget(self._section("Protección"))
        block_row = BoxLayout(size_hint_y=None, height=dp(48))
        block_row.add_widget(Label(text="Bloquear alta confianza", halign="left"))
        self.block_switch = Switch(active=get_blocking_enabled(), size_hint_x=None, width=dp(70))
        self.block_switch.bind(active=lambda _, value: set_blocking_enabled(value))
        block_row.add_widget(self.block_switch)
        body.add_widget(block_row)

        review_row = BoxLayout(size_hint_y=None, height=dp(48))
        review_row.add_widget(Label(text="Silenciar sospechosos", halign="left"))
        self.review_switch = Switch(active=get_silence_review_enabled(), size_hint_x=None, width=dp(70))
        self.review_switch.bind(active=lambda _, value: set_silence_review_enabled(value))
        review_row.add_widget(self.review_switch)
        body.add_widget(review_row)

        note = Label(
            text="[size=13sp]Sin READ_CONTACTS: Android sólo entrega al filtro llamadas de números que no estén en tus contactos.[/size]",
            markup=True, halign="left", valign="middle", size_hint_y=None, height=dp(64)
        )
        note.bind(size=lambda inst, value: setattr(inst, "text_size", value))
        body.add_widget(note)

        body.add_widget(self._section("Base GitHub"))
        self.url_input = TextInput(text=self.store.load_raw_base(), multiline=False, size_hint_y=None, height=dp(48), hint_text="https://raw.githubusercontent.com/usuario/repo/main/data")
        body.add_widget(self.url_input)
        save_btn = Button(text="Guardar URL", size_hint_y=None, height=dp(44))
        save_btn.bind(on_release=self._save_url)
        body.add_widget(save_btn)
        sync_btn = Button(text="Sincronizar ahora", size_hint_y=None, height=dp(50))
        sync_btn.bind(on_release=self._sync)
        body.add_widget(sync_btn)
        self.sync_label = Label(text="", markup=True, halign="left", valign="middle", size_hint_y=None, height=dp(58))
        self.sync_label.bind(size=lambda inst, value: setattr(inst, "text_size", value))
        body.add_widget(self.sync_label)

        body.add_widget(self._section("Consultar número"))
        self.phone_input = TextInput(hint_text="Ej. 612345678 o +34 612 345 678", multiline=False, input_type="phone", size_hint_y=None, height=dp(48))
        body.add_widget(self.phone_input)
        lookup_btn = Button(text="Consultar", size_hint_y=None, height=dp(48))
        lookup_btn.bind(on_release=self._lookup)
        body.add_widget(lookup_btn)
        self.lookup_label = Label(text="", markup=True, halign="left", valign="middle", size_hint_y=None, height=dp(72))
        self.lookup_label.bind(size=lambda inst, value: setattr(inst, "text_size", value))
        body.add_widget(self.lookup_label)

        self._refresh()
        raw_base = self.store.load_raw_base()
        if "USUARIO/REPOSITORIO" not in raw_base:
            configure_native_updates(raw_base)
            Clock.schedule_once(lambda _dt: self._sync(), 0.8)
        Clock.schedule_interval(lambda _dt: self._refresh_role_only(), 2.0)
        return root

    def _section(self, text):
        return Label(text=f"[b]{text}[/b]", markup=True, size_hint_y=None, height=dp(34), halign="left")

    def _refresh_role_only(self):
        held = is_screening_role_held()
        self.role_label.text = "[color=33cc66][b]● Protección Android activa[/b][/color]" if held else "[color=ff9966][b]● Falta activar el rol de filtrado[/b][/color]"

    def _refresh(self):
        self._refresh_role_only()
        stats = self.store.stats()
        self.stats_label.text = (
            f"[b]BLOCK:[/b] {stats.get('block_count', 0)} números\n"
            f"[b]REVIEW:[/b] {stats.get('review_count', 0)} números\n"
            f"[b]Última sincronización:[/b] {stats.get('last_sync') or 'Nunca'}\n"
            f"[b]Base generada:[/b] {stats.get('generated_at') or '—'}"
        )

    def _request_role(self, *_):
        if is_screening_role_held():
            self.sync_label.text = "El filtro de llamadas ya está activado."
            return
        self.sync_label.text = "Android ha abierto la solicitud del rol." if request_screening_role() else "No se pudo abrir la solicitud del rol."

    def _save_url(self, *_):
        try:
            self.store.save_raw_base(self.url_input.text)
            configure_native_updates(self.url_input.text)
            self.sync_label.text = "URL guardada y actualización automática programada."
        except Exception as exc:
            self.sync_label.text = f"[color=ff6666]Error: {exc}[/color]"

    def _sync(self, *_):
        try:
            self.store.save_raw_base(self.url_input.text)
            configure_native_updates(self.url_input.text)
        except Exception as exc:
            self.sync_label.text = f"[color=ff6666]URL inválida: {exc}[/color]"
            return
        self.sync_label.text = "Sincronizando y verificando SHA-256…"
        sync_async(self.syncer, self._sync_done, self.url_input.text)

    def _sync_done(self, result):
        Clock.schedule_once(lambda _dt: self._apply_sync_result(result), 0)

    def _apply_sync_result(self, result):
        if result.ok:
            self.sync_label.text = f"[color=33cc66]{result.message} BLOCK={result.block_count}, REVIEW={result.review_count}[/color]"
        else:
            self.sync_label.text = f"[color=ff6666]{result.message}[/color]"
        self._refresh()

    def _lookup(self, *_):
        status, message = self.store.lookup(self.phone_input.text)
        colors = {"BLOCK": "ff5555", "REVIEW": "ffaa33", "CLEAR": "55cc77", "INVALID": "ff6666"}
        self.lookup_label.text = f"[color={colors.get(status, 'ffffff')}][b]{status}[/b] — {message}[/color]"


if __name__ == "__main__":
    SpamGuardApp().run()
