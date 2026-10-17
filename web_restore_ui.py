"""Restore the existing native windows during an authorized maintenance restart."""
from game_capture import choose_monitor, get_monitors, overlap


def restore_ui(orb, saved):
    ui = saved['ui_windows']
    windows = ui['windows']
    monitors = get_monitors()
    orb.game_monitor = saved.get('capture', {}).get('monitor', {}).get('index')
    if orb.game_monitor is None:
        try:
            from game_capture import game_geometry
            orb.game_monitor = game_geometry()['monitor']['index']
        except Exception:
            pass

    def place(window, info):
        width, height = int(info['width']), int(info['height'])
        x, y = int(info['x']), int(info['y'])
        bounds = dict(left=x, top=y, width=width, height=height)
        if not any(overlap(bounds, monitor) > 0 for monitor in monitors):
            monitor = choose_monitor(bounds, monitors)
            x, y = monitor['left'], monitor['top']
        window.geometry(f'{width}x{height}')
        orb.position(window, x, y)
        state = info.get('state', 'normal')
        if state == 'iconic':
            window.iconify()
        elif state == 'withdrawn' or not info.get('visible'):
            window.withdraw()
        elif state == 'zoomed':
            window.state('zoomed')

    orb.alpha = float(ui['orb_alpha'])
    orb.ball.attributes('-alpha', orb.alpha)
    place(orb.ball, windows['orb'])
    for name, show in (('history', orb.show_history), ('combat', orb.show_combat),
                       ('multiscale', orb.show_multiscale)):
        info = windows.get(name, {})
        if info.get('exists'):
            show()
            place(getattr(orb, name+'_window').window, info)
    if windows.get('settings', {}).get('visible'):
        orb.show_settings()
    if windows.get('settings', {}).get('exists'):
        place(orb.root, windows['settings'])
    if windows.get('review', {}).get('exists'):
        orb.review_view.show_details()
        place(orb.review_view.window, windows['review'])
    # Auxiliary window actions fold the panel; restore it last.
    if windows.get('panel', {}).get('exists'):
        if orb.panel is None:
            orb.toggle_panel()
        if ui.get('combat_expanded'):
            orb.midnight_panel.set_expanded(True)
        if ui.get('settings_open'):
            orb.midnight_panel.toggle_options()
        place(orb.panel, windows['panel'])
    orb.save()
