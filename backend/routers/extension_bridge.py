from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Extension Bridge"])

@router.get("/extension/connect", response_class=HTMLResponse)
def extension_connect(handoff: str = Query(..., description="Full handoff URL to fetch")):
    # Page simple: l’extension l’intercepte; l’utilisateur n’a rien à coller.
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Connecting…</title>
  </head>
  <body style="font-family: sans-serif; padding: 24px;">
    <h3>Connecting…</h3>
    <p>If the extension is installed, it will open the connected profile automatically.</p>
    <p><b>handoff</b>: <code>{handoff}</code></p>
  </body>
</html>
"""

































# from fastapi import APIRouter, Request
# from fastapi.responses import HTMLResponse

# router = APIRouter(tags=["Extension Bridge"])

# @router.get("/extension/connect", response_class=HTMLResponse)
# def extension_connect(request: Request, handoff: str):
#     # This page only exists to trigger the extension (no secrets exposed here beyond the handoff URL).
#     return f"""
# <!doctype html>
# <html>
#   <head><meta charset="utf-8"><title>Connecting…</title></head>
#   <body style="font-family: sans-serif; padding: 20px;">
#     <h3>Connecting…</h3>
#     <p>You can close this tab if the extension opens the connected profile automatically.</p>
#     <p><b>handoff</b>: <code>{handoff}</code></p>
#   </body>
# </html>
# """