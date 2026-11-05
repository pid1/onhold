"""Cloudflare Workers entrypoint: the web app, plus the two cron jobs."""

import logging

from workers import WorkerEntrypoint, asgi

from onhold.app import asgi_app, build_service

log = logging.getLogger("onhold")

# wrangler.jsonc's triggers.crons, in the same order.
RECONCILE = "*/15 * * * *"
PURGE = "0 9 * * *"


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(asgi_app, request, self.env, self.ctx)

    async def scheduled(self, controller, *_):
        service = build_service(self.env)
        cron = getattr(controller, "cron", "")
        if cron == PURGE:
            result = await service.purge()
        else:
            result = await service.reconcile()
        log.warning("cron %s: %s", cron, result)
