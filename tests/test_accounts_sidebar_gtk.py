"""Actual sidebar block with synthetic cards on an isolated GTK display only."""
from tests import signal_safety as _signal_safety  # noqa: F401

import ast
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


@unittest.skipUnless(os.environ.get("TUXINDRIVE_GTK_TESTS") == "1", "Requires an isolated GTK display")
class AccountsSidebarGtkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, GLib, Gdk
        cls.Gtk, cls.GLib, cls.Gdk = Gtk, GLib, Gdk
        Gtk.init([])
        source = Path(__file__).resolve().parents[1] / "src/tuxindrive/app.py"
        tree = ast.parse(source.read_text())
        window = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MainWindow")
        constructor = next(node for node in window.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        start = next(i for i, node in enumerate(constructor.body) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "sidebar" for target in node.targets))
        end = next(i for i, node in enumerate(constructor.body[start:], start) if isinstance(node, ast.Expr)
                   and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                   and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "content"
                   and node.value.func.attr == "pack_start" and isinstance(node.value.args[0], ast.Name)
                   and node.value.args[0].id == "sidebar")
        # Exercise the production constructor's exact sidebar statements,
        # without importing/starting the application or its real accounts.
        cls.sidebar_code = compile(ast.Module(body=constructor.body[start:end + 1], type_ignores=[]), str(source), "exec")

    def pump(self):
        deadline = time.monotonic() + 0.08
        while time.monotonic() < deadline:
            while self.Gtk.events_pending():
                self.Gtk.main_iteration_do(False)
            time.sleep(0.002)

    def build(self, count, height=600):
        Gtk = self.Gtk
        window = Gtk.Window()
        self.addCleanup(window.destroy)
        window.set_default_size(900, height)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        window.add(content)
        controller = SimpleNamespace(_choose_provider=Mock())
        namespace = {"Gtk": Gtk, "GLib": self.GLib, "self": controller, "content": content,
                     "tr": lambda key: {"cloud_accounts": "Online accounts", "connect_account": "Add account"}[key]}
        exec(self.sidebar_code, namespace)
        sidebar = namespace["sidebar"]
        buttons = []
        for position in range(count):
            button = Gtk.Button(label=f"Synthetic account {position + 1:02d}")
            button.set_size_request(-1, 72)
            controller.account_list.add(button)
            buttons.append(button)
        main_scroll = Gtk.ScrolledWindow()
        main_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        jobs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for position in range(30):
            label = Gtk.Label(label=f"Synthetic synchronized folder {position + 1}")
            label.set_size_request(-1, 60)
            jobs.pack_start(label, False, False, 0)
        main_scroll.add(jobs)
        content.pack_start(main_scroll, True, True, 0)
        window.show_all()
        self.pump()
        return window, content, sidebar, controller, buttons, main_scroll

    def test_many_accounts_fit_window_and_last_card_scrolls_into_view_independently(self):
        window, content, sidebar, controller, buttons, main_scroll = self.build(40)
        scroll = controller.account_scroll
        adjustment = scroll.get_vadjustment()
        self.assertLessEqual(window.get_allocated_height(), 600)
        self.assertGreater(adjustment.get_upper(), adjustment.get_page_size())
        self.assertTrue(scroll.get_vscrollbar().get_child_visible())
        self.assertEqual(scroll.get_policy(), (self.Gtk.PolicyType.NEVER, self.Gtk.PolicyType.AUTOMATIC))
        main_scroll.get_vadjustment().set_value(120)
        main_value = main_scroll.get_vadjustment().get_value()
        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        self.pump()
        _x, y = buttons[-1].translate_coordinates(scroll, 0, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLess(y, scroll.get_allocated_height())
        self.assertEqual(main_scroll.get_vadjustment().get_value(), main_value)
        footer = sidebar.get_children()[-1]
        _x, footer_y = footer.translate_coordinates(content, 0, 0)
        self.assertGreaterEqual(footer_y, 0)
        self.assertLessEqual(footer_y + footer.get_allocated_height(), content.get_allocated_height())
        footer.clicked()
        controller._choose_provider.assert_called_once_with(footer)
        screenshot = os.environ.get("TUXINDRIVE_SIDEBAR_SCREENSHOT")
        if screenshot:
            self.Gdk.pixbuf_get_from_window(window.get_window(), 0, 0,
                                           window.get_allocated_width(), window.get_allocated_height()).savev(screenshot, "png", [], [])

    def test_few_or_no_accounts_do_not_show_an_unnecessary_scrollbar(self):
        for count in (0, 2):
            with self.subTest(count=count):
                window, _content, _sidebar, controller, _buttons, _main = self.build(count, height=420)
                adjustment = controller.account_scroll.get_vadjustment()
                self.assertLessEqual(window.get_allocated_height(), 420)
                self.assertLessEqual(adjustment.get_upper(), adjustment.get_page_size())
                self.assertFalse(controller.account_scroll.get_vscrollbar().get_child_visible())
                window.destroy()
                self.pump()
