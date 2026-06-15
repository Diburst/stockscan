"""Web route modules. Each page or feature gets its own router.

House rule: route handlers are plain ``def``, NOT ``async def``. Every
handler in this app does synchronous work (SQLAlchemy sessions, pandas) —
an ``async def`` handler would run that work ON the event loop, freezing
every other request for its duration (this is exactly how the old Fetch
Latest used to hang the whole UI). Sync handlers run in Starlette's
threadpool instead, so one slow request never blocks the app. Only declare
``async def`` if the handler genuinely awaits something.
"""
