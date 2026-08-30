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
from mcp.server.transport_security import TransportSecuritySettings

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


def _find_or_create_tag(tag_name: str) -> int:
    """Cherche une étiquette produit (product.tag) par nom, la crée si absente."""
    ids = odoo_execute("product.tag", "search", [["name", "=ilike", tag_name]], limit=1)
    if ids:
        return ids[0]
    return odoo_execute("product.tag", "create", {"name": tag_name})


def _find_sale_tax(tax_name: str) -> int | None:
    """Cherche une taxe de vente (account.tax) par nom approximatif (ex. '20%', 'TVA 20')."""
    ids = odoo_execute(
        "account.tax", "search",
        [["name", "ilike", tax_name], ["type_tax_use", "=", "sale"]],
        limit=1,
    )
    return ids[0] if ids else None


# Nom de domaine public de ce serveur (ex. odoo-mcp-kcxp.onrender.com), sans https:// ni slash.
# Sert à autoriser les requêtes venant de ce domaine (protection anti DNS-rebinding du SDK).
PUBLIC_HOSTNAME = os.environ.get("PUBLIC_HOSTNAME", "")

_allowed_hosts = ["127.0.0.1:*", "localhost:*"]
_allowed_origins = ["http://127.0.0.1:*", "http://localhost:*"]
if PUBLIC_HOSTNAME:
    _allowed_hosts.append(f"{PUBLIC_HOSTNAME}:*")
    _allowed_hosts.append(PUBLIC_HOSTNAME)
    _allowed_origins.append(f"https://{PUBLIC_HOSTNAME}")

mcp = FastMCP(
    "odoo",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


@mcp.tool()
def search_products(query: str, limit: int = 10) -> list[dict]:
    """Recherche des produits existants dans Odoo par nom OU par référence produit."""
    ids = odoo_execute(
        "product.template", "search",
        ["|", ["name", "ilike", query], ["default_code", "ilike", query]],
        limit=limit,
    )
    if not ids:
        return []
    return odoo_execute(
        "product.template", "read", ids,
        fields=["id", "name", "list_price", "standard_price", "default_code",
                 "taxes_id", "product_tag_ids"],
    )


@mcp.tool()
def create_product(
    name: str,
    price: float,
    cost: float = None,
    tax_name: str = None,
    tag: str = None,
    description: str = "",
) -> dict:
    """Crée un nouveau produit vendable dans Odoo.

    Args:
        name: nom du produit
        price: prix de vente unitaire (HT)
        cost: prix d'achat unitaire, si connu
        tax_name: nom ou taux de la taxe de vente à appliquer (ex. "20%", "TVA 5.5%").
            Doit correspondre à une taxe déjà configurée dans Odoo ; si aucune
            correspondance n'est trouvée, le produit est créé sans taxe spécifique.
        tag: étiquette de catégorisation, généralement le fournisseur (ex. "Rexel",
            "Cedeo", "123elec"). Créée automatiquement si elle n'existe pas encore.
        description: description commerciale optionnelle
    """
    vals = {
        "name": name,
        "list_price": price,
        "sale_ok": True,
        "description_sale": description,
    }
    if cost is not None:
        vals["standard_price"] = cost

    warnings = []

    if tax_name:
        tax_id = _find_sale_tax(tax_name)
        if tax_id:
            vals["taxes_id"] = [(6, 0, [tax_id])]
        else:
            warnings.append(f"Taxe '{tax_name}' introuvable, produit créé sans taxe spécifique.")

    if tag:
        tag_id = _find_or_create_tag(tag)
        vals["product_tag_ids"] = [(6, 0, [tag_id])]

    product_id = odoo_execute("product.template", "create", vals)
    result = {"id": product_id, "name": name, "price": price}
    if warnings:
        result["warnings"] = warnings
    return result


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


@mcp.tool()
def update_product(
    product_id: int,
    name: str = None,
    price: float = None,
    cost: float = None,
    tax_name: str = None,
    tag: str = None,
    description: str = None,
) -> dict:
    """Modifie une fiche produit existante dans Odoo.

    Seuls les champs fournis (non None) sont modifiés ; les autres restent inchangés.

    Args:
        product_id: identifiant du produit à modifier (obtenu via search_products)
        name: nouveau nom, si à changer
        price: nouveau prix de vente, si à changer
        cost: nouveau prix d'achat, si à changer
        tax_name: nom ou taux de la taxe de vente à appliquer (remplace la taxe existante)
        tag: étiquette / fournisseur à associer (remplace l'étiquette existante ;
            créée automatiquement si elle n'existe pas encore)
        description: nouvelle description commerciale, si à changer
    """
    vals = {}
    if name is not None:
        vals["name"] = name
    if price is not None:
        vals["list_price"] = price
    if cost is not None:
        vals["standard_price"] = cost
    if description is not None:
        vals["description_sale"] = description

    warnings = []

    if tax_name is not None:
        tax_id = _find_sale_tax(tax_name)
        if tax_id:
            vals["taxes_id"] = [(6, 0, [tax_id])]
        else:
            warnings.append(f"Taxe '{tax_name}' introuvable, taxe inchangée.")

    if tag is not None:
        tag_id = _find_or_create_tag(tag)
        vals["product_tag_ids"] = [(6, 0, [tag_id])]

    if not vals:
        return {"error": "Aucun champ à modifier n'a été fourni."}

    odoo_execute("product.template", "write", [product_id], vals)
    updated = odoo_execute(
        "product.template", "read", [product_id],
        fields=["id", "name", "list_price", "standard_price", "default_code",
                 "description_sale", "taxes_id", "product_tag_ids"],
    )
    result = updated[0] if updated else {"error": "Produit introuvable après modification."}
    if warnings:
        result["warnings"] = warnings
    return result


@mcp.tool()
def find_duplicate_products() -> list[dict]:
    """Identifie les groupes de produits potentiellement en double dans Odoo
    (même nom, insensible à la casse et aux espaces superflus).

    Retourne une liste de groupes ; chaque groupe contient les produits
    partageant le même nom, avec leur id, prix et référence, pour te
    permettre de choisir lesquels garder ou supprimer via delete_product.
    N'effectue AUCUNE suppression automatique.
    """
    all_ids = odoo_execute("product.template", "search", [])
    if not all_ids:
        return []
    records = odoo_execute(
        "product.template", "read", all_ids,
        fields=["id", "name", "list_price", "default_code"],
    )

    groups: dict[str, list[dict]] = {}
    for rec in records:
        key = " ".join(rec["name"].strip().lower().split())
        groups.setdefault(key, []).append(rec)

    duplicates = [
        {"name": group[0]["name"], "products": group}
        for group in groups.values()
        if len(group) > 1
    ]
    return duplicates


@mcp.tool()
def delete_product(product_id: int) -> dict:
    """Supprime définitivement un produit d'Odoo.

    À utiliser avec précaution : la suppression est irréversible. Utilise
    find_duplicate_products ou search_products au préalable pour confirmer
    l'identifiant exact avant de supprimer.

    Args:
        product_id: identifiant du produit à supprimer
    """
    odoo_execute("product.template", "unlink", [product_id])
    return {"deleted_id": product_id}


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
