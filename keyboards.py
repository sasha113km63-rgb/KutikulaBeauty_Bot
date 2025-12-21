def inline_keyboard(buttons: list[tuple[str, str]], row: int = 2) -> dict:
    rows = []
    for i in range(0, len(buttons), row):
        rows.append([{"text": t, "callback_data": d} for t, d in buttons[i:i+row]])
    return {"inline_keyboard": rows}
