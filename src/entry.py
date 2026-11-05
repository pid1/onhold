"""Cloudflare Workers entrypoint: the web app, plus the cron job."""

import logging

from workers import WorkerEntrypoint, asgi

from onhold.app import asgi_app, build_service

log = logging.getLogger("onhold")


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(asgi_app, request, self.env, self.ctx)

    async def scheduled(self, controller, *_):
        """Every 15 minutes (wrangler.jsonc): reconcile, then purge. Both are cheap."""
        service = build_service(self.env)
        reconciled = await service.reconcile()
        purged = await service.purge()
        log.warning("cron: reconciled %s, purged %s", reconciled, purged)
