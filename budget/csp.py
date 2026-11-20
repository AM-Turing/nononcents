"""Content-Security-Policy header (Django 5.2 has none built in).

Everything the pages load comes from this site: scripts and fonts are static files, there are
no inline scripts or event-handler attributes, and no CDN. The only inline allowance is for
style *attributes* (``style-src-attr``): the legend and chart colour dots set a colour per row
(``style="--legend-color:..."``). Style sheets and ``<style>`` blocks stay restricted to 'self'.
"""

POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "style-src-attr 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    ]
)


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", POLICY)
        return response
