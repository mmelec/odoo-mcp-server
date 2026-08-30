"""
Serveur MCP Odoo — version hébergée en ligne (transport HTTP).

Contrairement à server.py (version locale), celui-ci tourne comme un
service web que Claude (claude.ai, Claude Desktop, Cowork, mobile...)
contacte directement sur internet, sans rien installer sur ta machine.

Protégé par une clé d'accès (SERVER_API_KEY) : sans elle, personne ne
peut utiliser tes outils Odoo même s'il trouve l'URL du serveur.
"""

import os
import xmlrpc.client

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

# --- Configuration Odoo (lue depuis les variables d'environnement / secrets) ---
ODOO_URL = os.environ["ODOO_URL"].rstrip("/")
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USERNAME = os.environ["ODOO_USERNAME"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

# Clé pour protéger CE serveur (différente de la clé API Odoo).
# Choisis une chaîne longue et aléatoire, à mettre aussi côté Claude.
SERVER_API_KEY = os.environ["SERVER_API_KEY"]

# --- Connexion XML-RPC à Odoo ---
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

UID = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
if not UID:
    raise RuntimeError(
        "Authentification Odoo échouée. Vérifie ODOO_URL, ODOO_DB, "
        "ODOO_USERNAME et ODOO_API_KEY."
    )


def odoo_execute(model, method, *args, **kwargs):
    return models.execute_kw(
        ODOO_DB, UID, ODOO_API_KEY, model, method, list(args), kwargs
    )


mcp = FastMCP("odoo")


@mcp.tool()
def search_products(query: str, limit: int = 10) -> list[dict]:
    """Recherche des produits existants dans Odoo par nom."""
    ids = odoo_execute("product.template", "search", [["name", "ilike", query]], limit=limit)
    if not ids:
        return []
    return odoo_execute(
        "product.template", "read", ids,
        fields=["id", "name", "list_price", "default_code"],
    )


@mcp.tool()
def create_product(name: str, price: float, description: str = "") -> dict:
    """Crée un nouveau produit vendable dans Odoo."""
    product_id = odoo_execute(
        "product.template", "create",
        {"name": name, "list_price": price, "sale_ok": True, "description_sale": description},
    )
    return {"id": product_id, "name": name, "price": price}


@mcp.tool()
def search_partner(query: str, limit: int = 10) -> list[dict]:
    """Recherche un client / contact existant dans Odoo par nom."""
    ids = odoo_execute("res.partner", "search", [["name", "ilike", query]], limit=limit)
    if not ids:
        return []
    return odoo_execute("res.partner", "read", ids, fields=["id", "name", "email", "phone"])


@mcp.tool()
def create_partner(name: str, email: str = "", phone: str = "") -> dict:
    """Crée un nouveau contact / client dans Odoo."""
    partner_id = odoo_execute("res.partner", "create", {"name": name, "email": email, "phone": phone})
    return {"id": partner_id, "name": name}


@mcp.tool()
def create_quote(partner_id: int, lines: list[dict]) -> dict:
    """Crée un devis (sale.order) dans Odoo pour un client donné.

    lines: liste de dicts avec product_id, quantity, et price_unit (optionnel).
    """
    order_lines = []
    for line in lines:
        vals = {"product_id": line["product_id"], "product_uom_qty": line.get("quantity", 1)}
        if "price_unit" in line:
            vals["price_unit"] = line["price_unit"]
        order_lines.append((0, 0, vals))

    order_id = odoo_execute(
        "sale.order", "create", {"partner_id": partner_id, "order_line": order_lines}
    )
    return odoo_execute(
        "sale.order", "read", [order_id], fields=["id", "name", "amount_total", "state"]
    )[0]


# --- Protection par clé d'accès ---
class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        supplied = request.query_params.get("api_key") or request.headers.get("x-api-key")
        if supplied != SERVER_API_KEY:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(ApiKeyMiddleware)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)