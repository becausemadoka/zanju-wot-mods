# -*- coding: utf-8 -*-
"""Tests for the teardown of the tooltip view.

The tooltip is a second Scaleform view on a band of its own, and the module global
`_tooltip_view` is the only thing that holds it. A lobby exit that leaves it standing does not
merely break the tooltip. The view outlives the lobby that loaded it, the client disposes it
during the NEXT lobby teardown, and the client then crashes with an access violation on the way
into battle. That crash reaches Python as nothing at all, so these tests are the only place the
rule is written.

`hooks` and `sync` both reach the client at import time, so this module registers the stubs it
needs before it imports them. The fake `View.destroy` calls `_dispose`, because that is the
order game.log shows: "Tooltip view disposed" always comes before "Disposed tooltip view".
"""
from __future__ import unicode_literals

import logging
import sys
import types
import unittest


def _register(name, **attributes):
    """Register a stub client module, and the empty packages above it."""
    parts = name.split('.')
    for depth in range(1, len(parts)):
        parent_name = '.'.join(parts[:depth])
        if parent_name not in sys.modules:
            package = types.ModuleType(str(parent_name))
            package.__path__ = []
            sys.modules[parent_name] = package
        if depth > 1:
            setattr(sys.modules['.'.join(parts[:depth - 1])], parts[depth - 1], sys.modules[parent_name])
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(str(name))
        sys.modules[name] = module
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    if len(parts) > 1:
        setattr(sys.modules['.'.join(parts[:-1])], parts[-1], module)
    return module


class FakeView(object):
    """Stands in for the client's Scaleform `View`."""

    def __init__(self):
        self.destroy_calls = 0

    def _populate(self):
        pass

    def _dispose(self):
        pass

    def _isDAAPIInited(self):
        # False keeps every `as_*S` wrapper off `flashObject`, which no stub supplies.
        return False

    def destroy(self):
        self.destroy_calls += 1
        self._dispose()


class RaisingView(FakeView):
    """A view whose teardown fails. The client does this when its app is already gone."""

    def destroy(self):
        self.destroy_calls += 1
        raise RuntimeError('the movie is gone')


class FakeTooltipView(FakeView):
    """A loaded tooltip view, recording what it was asked to draw."""

    def __init__(self):
        FakeView.__init__(self)
        self.shown = []
        self.hidden = 0

    def as_showTooltipS(self, entries, cursor_x, cursor_y):
        self.shown.append((entries, cursor_x, cursor_y))

    def as_hideTooltipS(self):
        self.hidden += 1


class FakeScheduler(object):
    """Stands in for `BigWorld.callback`, so a test can fire a pending release on demand."""

    def __init__(self):
        self.pending = {}
        self.next_id = 1

    def callback(self, delay, fn):
        callback_id = self.next_id
        self.next_id += 1
        self.pending[callback_id] = fn
        return callback_id

    def cancelCallback(self, callback_id):
        self.pending.pop(callback_id, None)

    def fire_all(self):
        due = list(self.pending.values())
        self.pending.clear()
        for fn in due:
            fn()


_scheduler = FakeScheduler()


def _constant(name, **members):
    return type(str(name), (object,), members)


# Only the distinctness of these band numbers matters here, not the client's own values.
_WINDOW_LAYER = _constant(
    'WindowLayer', SUB_VIEW=5, TOP_SUB_VIEW=6, WINDOW=7, FULLSCREEN_WINDOW=8, SYSTEM_MESSAGE=9,
    TOP_WINDOW=10, OVERLAY=11,
)
_SPACE_ID = _constant('GuiGlobalSpaceID', LOBBY=1, BATTLE=2)

_register('BigWorld', callback=_scheduler.callback, cancelCallback=_scheduler.cancelCallback)
_register('CurrentVehicle', g_currentVehicle=object(), g_currentPreviewVehicle=object())
_register('frameworks.wulf', WindowLayer=_WINDOW_LAYER)
_register(
    'gui.Scaleform.framework',
    ScopeTemplates=_constant('ScopeTemplates', GLOBAL_SCOPE=0),
    ViewSettings=lambda *args, **kwargs: None,
    g_entitiesFactories=_constant('Factories', addSettings=staticmethod(lambda settings: None))(),
)
_register('gui.Scaleform.framework.entities.View', View=FakeView)
_register('gui.Scaleform.framework.managers.loaders', SFViewLoadParams=lambda alias: alias)
_register('gui.shared.personality', ServicesLocator=_constant('ServicesLocator', appLoader=None))
_register('helpers', dependency=_constant('dependency', instance=staticmethod(lambda skeleton: None)))
_register('skeletons.gui.shared', IItemsCache=object)
_register('skeletons.gui.app_loader', GuiGlobalSpaceID=_SPACE_ID)

from zanju_rpb.constants import SCALEFORM_VIEW_ALIAS, TOOLTIP_VIEW_ALIAS  # noqa: E402
from zanju_rpb.scaleform import hooks, runtime, sync  # noqa: E402  (stubs registered above)


class LoadedTooltipView(hooks._ScaleformTooltipView):
    """A real `_ScaleformTooltipView` over the stub `View`, so `_populate` runs as it does in game."""

    def __init__(self):
        hooks._ScaleformTooltipView.__init__(self)
        self.shown = []
        self.hidden = 0

    def as_showTooltipS(self, entries, cursor_x, cursor_y):
        self.shown.append((entries, cursor_x, cursor_y))

    def as_hideTooltipS(self):
        self.hidden += 1


class SilentLogger(logging.Logger):

    def __init__(self):
        logging.Logger.__init__(self, 'test')
        self.infos = []
        self.exceptions = []
        self.addHandler(logging.NullHandler())

    def info(self, message, *args, **kwargs):
        self.infos.append(message % args if args else message)

    def exception(self, message, *args, **kwargs):
        self.exceptions.append(message % args if args else message)


class FakeMod(object):
    """The attributes of the mod that the two teardown paths read and write."""

    def __init__(self):
        self._scaleform_view = None
        self._scaleform_view_visible = None
        self._scaleform_view_requested = True
        self._scaleform_payload = {'modes': []}
        self._scaleform_container_manager = None
        self._scaleform_hooks_registered = False
        self._scaleform_settings_registered = False
        self._current_lobby_route_path = 'subScope/subLayer/hangar/{root}'
        self._last_seen_sub_view_alias = 'hangar'
        self._on_view_added_to_container = lambda view: None
        self._on_gui_space_entered = lambda space_id: None
        self._on_gui_space_left = lambda space_id: None
        self.probes_cancelled = 0

    def _cancel_visibility_probe(self):
        self.probes_cancelled += 1


class TooltipDisposalTest(unittest.TestCase):

    def setUp(self):
        self.logger = SilentLogger()
        self.view = FakeView()
        hooks._tooltip_view = self.view

    def tearDown(self):
        hooks._tooltip_view = None

    def test_disposal_destroys_the_view_and_releases_it(self):
        self.assertTrue(hooks._dispose_tooltip_view('test', self.logger))
        self.assertEqual(1, self.view.destroy_calls)
        self.assertIsNone(hooks._tooltip_view)

    def test_disposal_releases_the_view_even_when_the_teardown_fails(self):
        # A destroy that raises used to leave the global pointed at a dead view. The next
        # hover then called into it, which is the same access violation by another route.
        hooks._tooltip_view = RaisingView()
        self.assertFalse(hooks._dispose_tooltip_view('test', self.logger))
        self.assertIsNone(hooks._tooltip_view)
        self.assertEqual(1, len(self.logger.exceptions))

    def test_disposal_with_no_view_is_quiet(self):
        hooks._tooltip_view = None
        self.assertFalse(hooks._dispose_tooltip_view('test', self.logger))

    def test_a_hover_after_disposal_draws_nothing(self):
        hooks._dispose_tooltip_view('test', self.logger)
        # Reaches the guard rather than the dead view. Without the release above, this call
        # is what crashed the client.
        hooks._show_tooltip('0', 100, 200)


class TooltipBandTest(unittest.TestCase):
    """The band the tooltip is registered on is not a free choice."""

    # Bands on which the client's lobby app was seen to carry a Scaleform view, over a two-day
    # log: `SFWindow` appeared on each of these and on no other. A Scaleform view needs a
    # container on its band, and a band with no container is not merely empty -- the view does
    # not appear, and the failure reaches the bar.
    SCALEFORM_BANDS = (3, 4, 5, 6, 7, 10, 16)

    def test_the_tooltip_sits_on_a_band_that_carries_scaleform_views(self):
        # Band 9 is the one gap in the notification-blocking list, so it reads better on paper,
        # and it was tried in game on 2.4. The whole bar vanished. Do not try it again.
        self.assertIn(hooks.TOOLTIP_LAYER, self.SCALEFORM_BANDS)

    def test_the_tooltip_still_clears_the_band_the_bar_is_on(self):
        # Clearing band 7 is the whole reason the tooltip is a view of its own.
        self.assertGreater(hooks.TOOLTIP_LAYER, hooks.VIEW_LAYER)


class FakeApp(object):
    """The lobby app, as far as a view load request can tell. `SFViewLoadParams` is the alias."""

    def __init__(self, fail_alias=None):
        self.loaded = []
        self.fail_alias = fail_alias

    def loadView(self, params):
        self.loaded.append(params)
        if params == self.fail_alias:
            raise RuntimeError('no container on that band')


class TooltipFailureIsolationTest(unittest.TestCase):
    """A tooltip that cannot load costs the hover text. It must never cost the bar."""

    def setUp(self):
        self.logger = SilentLogger()

    def test_a_tooltip_that_cannot_load_does_not_raise_into_the_hover(self):
        # The hover arrives on the bar's view. An exception escaping here would surface on every
        # frame the cursor rests on a marker.
        hooks._tooltip_markers_by_index = {1: {'costXp': 500}}
        hooks.ServicesLocator.appLoader = _constant(
            'AppLoader', getDefLobbyApp=staticmethod(lambda: FakeApp(fail_alias=TOOLTIP_VIEW_ALIAS)))
        try:
            hooks._show_tooltip('1', 10, 20)
        finally:
            hooks._tooltip_markers_by_index = {}
            hooks.ServicesLocator.appLoader = None
            hooks._tooltip_load_requested = False
            hooks._pending_tooltip_hover = None

    def test_a_bar_that_cannot_load_is_reported_as_not_requested(self):
        app = FakeApp(fail_alias=SCALEFORM_VIEW_ALIAS)
        self.assertFalse(
            runtime._request_scaleform_view_load(True, True, None, False, app, 'test', self.logger)
        )

    def test_the_bar_registers_even_when_the_tooltip_settings_fail(self):
        # This is the shape that took the bar off the screen: the exception escaped to
        # `_start_scaleform_view_runtime`, which then never attached its space hooks.
        def boom():
            raise RuntimeError('bad band')

        original = hooks._register_tooltip_view_settings
        hooks._register_tooltip_view_settings = boom
        try:
            registered = hooks._register_scaleform_view_settings(False, 'alias', FakeView, 'file.swf')
        finally:
            hooks._register_tooltip_view_settings = original
        self.assertTrue(registered)


class HoverLifetimeTest(unittest.TestCase):
    """The tooltip is loaded for the length of a hover, not the length of the garage session.

    A window loaded on `TOOLTIP_LAYER` holds back the client's reward and event queue. Keeping
    this view for the session left the "important events missed" notice inert for as long as the
    player stayed in the garage, and clicking it did nothing.
    """

    def setUp(self):
        self.logger = SilentLogger()
        _scheduler.pending.clear()
        hooks._tooltip_view = None
        hooks._pending_tooltip_hover = None
        hooks._tooltip_load_requested = False
        hooks._tooltip_release_callback = None
        hooks._tooltip_markers_by_index = {1: {'costXp': 500, 'tooltipCombatXp': 10, 'tooltipFreeXp': 2}}
        self.app = FakeApp()
        hooks.ServicesLocator.appLoader = _constant('AppLoader', getDefLobbyApp=staticmethod(lambda: self.app))

    def tearDown(self):
        hooks._tooltip_view = None
        hooks._pending_tooltip_hover = None
        hooks._tooltip_load_requested = False
        hooks._tooltip_release_callback = None
        hooks._tooltip_markers_by_index = {}
        hooks.ServicesLocator.appLoader = None
        _scheduler.pending.clear()

    def test_a_hover_with_no_view_loaded_asks_for_one(self):
        hooks._show_tooltip('1', 100, 200)
        self.assertEqual([TOOLTIP_VIEW_ALIAS], self.app.loaded)
        self.assertIsNotNone(hooks._pending_tooltip_hover)

    def test_a_resting_cursor_asks_only_once(self):
        # The bar reports a hover every frame. Without the guard this would load every frame.
        for _ in range(5):
            hooks._show_tooltip('1', 100, 200)
        self.assertEqual([TOOLTIP_VIEW_ALIAS], self.app.loaded)

    def test_the_held_hover_is_drawn_when_the_view_arrives(self):
        # The load is asynchronous, so the hover that asked for it has to survive the wait.
        hooks._show_tooltip('1', 100, 200)
        view = LoadedTooltipView()
        view._populate()
        self.assertEqual(1, len(view.shown))
        self.assertEqual((100, 200), view.shown[0][1:])
        self.assertIsNone(hooks._pending_tooltip_hover)

    def test_the_first_draw_is_sent_again_once_the_view_is_attached(self):
        # The view is not on the stage when `_populate` runs, so the draw there can be held by
        # the ActionScript side. Nothing resends a hover on its own: the bar reports one only
        # when the marker set changes, so a lost first draw stays lost until the cursor leaves
        # the marker and comes back. That was the blank first tooltip on every vehicle.
        hooks._show_tooltip('1', 100, 200)
        view = LoadedTooltipView()
        view._populate()
        self.assertEqual(1, len(view.shown))
        _scheduler.fire_all()
        self.assertEqual(2, len(view.shown))
        self.assertEqual(view.shown[0], view.shown[1])

    def test_the_resend_is_dropped_when_the_view_is_already_gone(self):
        hooks._show_tooltip('1', 100, 200)
        view = LoadedTooltipView()
        view._populate()
        hooks._tooltip_view = None
        _scheduler.fire_all()
        self.assertEqual(1, len(view.shown))

    def test_a_view_that_arrives_after_the_cursor_left_is_released(self):
        hooks._show_tooltip('1', 100, 200)
        hooks._show_tooltip('', 100, 200)
        view = LoadedTooltipView()
        view._populate()
        self.assertEqual(0, len(view.shown))
        _scheduler.fire_all()
        self.assertEqual(1, view.destroy_calls)

    def test_leaving_the_markers_releases_the_view(self):
        view = FakeTooltipView()
        hooks._tooltip_view = view
        hooks._show_tooltip('', 100, 200)
        self.assertEqual(1, view.hidden)
        # Not destroyed yet: a sweep across the bar must not load and destroy it repeatedly.
        self.assertEqual(0, view.destroy_calls)
        _scheduler.fire_all()
        self.assertEqual(1, view.destroy_calls)
        self.assertIsNone(hooks._tooltip_view)

    def test_a_hover_arriving_during_the_delay_keeps_the_view(self):
        view = FakeTooltipView()
        hooks._tooltip_view = view
        hooks._show_tooltip('', 100, 200)
        hooks._show_tooltip('1', 110, 210)
        _scheduler.fire_all()
        self.assertEqual(0, view.destroy_calls)
        self.assertEqual(1, len(view.shown))

    def test_the_bar_no_longer_loads_the_tooltip_alongside_itself(self):
        app = FakeApp()
        runtime._request_scaleform_view_load(True, True, None, False, app, 'test', self.logger)
        self.assertEqual([SCALEFORM_VIEW_ALIAS], app.loaded)


class MarkerContextLifetimeTest(unittest.TestCase):
    """`_tooltip_markers_by_index` is module state, so its lifetime has to be owned by something.

    It holds plain dicts rather than a view handle, so a stale map cannot crash the client the
    way a stale view did. It can do something quieter and worse: resolve a hover against the
    previous vehicle and state that vehicle's XP as fact.
    """

    CONTEXT = {'modes': [{'markers': [{'tooltipIndex': 1, 'costXp': 500}]}]}

    def setUp(self):
        hooks._tooltip_markers_by_index = {}

    def tearDown(self):
        hooks._tooltip_markers_by_index = {}

    def test_the_context_is_remembered(self):
        hooks._remember_tooltip_context(self.CONTEXT)
        self.assertEqual([1], list(hooks._tooltip_markers_by_index))

    def test_the_bar_going_away_takes_the_context_with_it(self):
        hooks._remember_tooltip_context(self.CONTEXT)
        view = hooks._ScaleformGarageView()
        view._dispose()
        self.assertEqual({}, hooks._tooltip_markers_by_index)

    def test_releasing_the_tooltip_keeps_the_context(self):
        # The tooltip is released between hovers of the same vehicle. Clearing the markers on
        # that path would leave every hover after the first with nothing to draw.
        hooks._remember_tooltip_context(self.CONTEXT)
        hooks._tooltip_view = FakeTooltipView()
        hooks._dispose_tooltip_view('hover_left', SilentLogger())
        self.assertEqual([1], list(hooks._tooltip_markers_by_index))

    def test_a_context_that_cannot_be_read_empties_rather_than_goes_stale(self):
        hooks._remember_tooltip_context(self.CONTEXT)
        # `int(None)` inside the loop: the bar has the new context, so the old one must not stay.
        hooks._remember_tooltip_context({'modes': [{'markers': [{'tooltipIndex': object()}]}]})
        self.assertEqual({}, hooks._tooltip_markers_by_index)


class LobbyExitTest(unittest.TestCase):
    """The crash path: a disconnect leaves the lobby without destroying its app."""

    def setUp(self):
        self.logger = SilentLogger()
        self.mod = FakeMod()
        self.view = FakeView()
        hooks._tooltip_view = self.view

    def tearDown(self):
        hooks._tooltip_view = None

    def test_leaving_the_lobby_disposes_the_tooltip(self):
        sync._handle_gui_space_left(self.mod, _SPACE_ID.LOBBY, self.logger)
        self.assertEqual(1, self.view.destroy_calls)
        self.assertIsNone(hooks._tooltip_view)

    def test_leaving_the_lobby_disposes_the_bar_as_well(self):
        bar = FakeView()
        self.mod._scaleform_view = bar
        sync._handle_gui_space_left(self.mod, _SPACE_ID.LOBBY, self.logger)
        self.assertEqual(1, bar.destroy_calls)
        self.assertIsNone(self.mod._scaleform_view)

    def test_leaving_any_other_space_leaves_the_tooltip_alone(self):
        sync._handle_gui_space_left(self.mod, _SPACE_ID.BATTLE, self.logger)
        self.assertEqual(0, self.view.destroy_calls)
        self.assertIs(self.view, hooks._tooltip_view)

    def test_stopping_the_mod_disposes_the_tooltip(self):
        sync._stop_scaleform_view(self.mod, self.logger)
        self.assertEqual(1, self.view.destroy_calls)
        self.assertIsNone(hooks._tooltip_view)


if __name__ == '__main__':
    unittest.main()
