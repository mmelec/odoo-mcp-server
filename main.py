"""
Serveur MCP Odoo — version hébergée en ligne (transport HTTP).

Contrairement à server.py (version locale), celui-ci tourne comme un
service web que Claude (claude.ai, Claude Desktop, Cowork, mobile...)
contacte directement sur internet, sans rien installer sur ta machine.

Protégé par une clé d'accès (SERVER_API_KEY) : sans elle, personne ne
peut utiliser tes outils Odoo même s'il trouve l'URL du serveur.
"""

import math
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


def _find_tax(tax_name: str, tax_use: str) -> int | None:
    """Cherche une taxe (account.tax) par nom approximatif (ex. '20%', 'TVA 20').

    tax_use: 'sale' pour une taxe de vente, 'purchase' pour une taxe d'achat.
    """
    ids = odoo_execute(
        "account.tax", "search",
        [["name", "ilike", tax_name], ["type_tax_use", "=", tax_use]],
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
                 "taxes_id", "supplier_taxes_id", "product_tag_ids"],
    )


@mcp.tool()
def create_product(
    name: str,
    price: float,
    price_tax_name: str = None,
    cost: float = None,
    cost_tax_name: str = None,
    tag: str = None,
    description: str = "",
    reference: str = None,
) -> dict:
    """Crée un nouveau produit vendable dans Odoo.

    Args:
        reference: référence interne (default_code), ex. référence Yesss
        name: nom du produit
        price: prix de vente unitaire (HT)
        price_tax_name: nom ou taux de la taxe de VENTE à appliquer sur le prix
            de vente (ex. "20%", "TVA 5.5%"). Doit correspondre à une taxe déjà
            configurée dans Odoo (type "Vente") ; sinon aucune taxe n'est fixée.
        cost: prix d'achat unitaire, si connu
        cost_tax_name: nom ou taux de la taxe d'ACHAT à appliquer sur le prix
            d'achat. Doit correspondre à une taxe déjà configurée dans Odoo
            (type "Achat") ; sinon aucune taxe n'est fixée.
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
    if reference:
        vals["default_code"] = reference

    warnings = []

    if price_tax_name:
        tax_id = _find_tax(price_tax_name, "sale")
        if tax_id:
            vals["taxes_id"] = [(6, 0, [tax_id])]
        else:
            warnings.append(f"Taxe de vente '{price_tax_name}' introuvable.")

    if cost_tax_name:
        tax_id = _find_tax(cost_tax_name, "purchase")
        if tax_id:
            vals["supplier_taxes_id"] = [(6, 0, [tax_id])]
        else:
            warnings.append(f"Taxe d'achat '{cost_tax_name}' introuvable.")

    if tag:
        tag_id = _find_or_create_tag(tag)
        vals["product_tag_ids"] = [(6, 0, [tag_id])]

    product_id = odoo_execute("product.template", "create", vals)
    result = {"id": product_id, "name": name, "price": price}
    if warnings:
        result["warnings"] = warnings
    return result


@mcp.tool()
def create_products(
    products: list[dict],
    price_tax_name: str = "10% G",
    cost_tax_name: str = "20%",
    tag: str = "Yesss",
    dry_run: bool = False,
) -> dict:
    """Crée plusieurs produits d'un coup (import fournisseur).

    Chaque produit : {reference, name, price, cost}. Le nom final est
    "<name> - <reference>". Les références (default_code) déjà présentes dans
    Odoo sont ignorées (pas de doublon).

    Args:
        products: liste de {reference, name, price, cost}
        price_tax_name: taxe de vente (défaut "10% G")
        cost_tax_name: taxe d'achat (défaut "20%")
        tag: étiquette fournisseur (défaut "Yesss")
        dry_run: si True, ne crée rien et indique ce qui serait fait
    """
    created, skipped, errors = [], [], []
    sale_tax = _find_tax(price_tax_name, "sale") if price_tax_name else None
    buy_tax = _find_tax(cost_tax_name, "purchase") if cost_tax_name else None
    warnings = []
    if price_tax_name and not sale_tax:
        warnings.append(f"Taxe de vente '{price_tax_name}' introuvable.")
    if cost_tax_name and not buy_tax:
        warnings.append(f"Taxe d'achat '{cost_tax_name}' introuvable.")
    tag_id = None if (dry_run or not tag) else _find_or_create_tag(tag)

    seen = set()
    for p in products:
        ref = (p.get("reference") or "").strip()
        label = (p.get("name") or "").strip()
        try:
            if not ref or not label:
                raise ValueError("reference et name obligatoires")
            if ref in seen:
                skipped.append({"reference": ref, "reason": "doublon dans la liste"})
                continue
            seen.add(ref)
            existing = odoo_execute(
                "product.template", "search_read",
                [["default_code", "=", ref]], fields=["id", "name"], limit=1,
            )
            if existing:
                skipped.append({"reference": ref, "id": existing[0]["id"],
                                "reason": "existe déjà"})
                continue
            price = float(p["price"]) if p.get("price") is not None else None
            cost = float(p["cost"]) if p.get("cost") is not None else None
            if price is None and cost is not None:
                price = round(cost * 1.4, 2)
            if price is None:
                raise ValueError("price ou cost requis")
            full_name = f"{label} - {ref}"
            if dry_run:
                created.append({"reference": ref, "name": full_name,
                                "price": price, "cost": cost})
                continue
            vals = {
                "name": full_name, "default_code": ref, "list_price": price,
                "sale_ok": True,
            }
            if cost is not None:
                vals["standard_price"] = cost
            if sale_tax:
                vals["taxes_id"] = [(6, 0, [sale_tax])]
            if buy_tax:
                vals["supplier_taxes_id"] = [(6, 0, [buy_tax])]
            if tag_id:
                vals["product_tag_ids"] = [(6, 0, [tag_id])]
            pid = odoo_execute("product.template", "create", vals)
            created.append({"id": pid, "reference": ref, "name": full_name,
                            "price": price, "cost": cost})
        except Exception as e:  # noqa: BLE001
            errors.append({"reference": ref, "error": str(e)})

    result = {"dry_run": dry_run, "created": created, "skipped_existing": skipped,
              "errors": errors,
              "summary": f"{len(created)} à créer/créés, {len(skipped)} ignorés, {len(errors)} erreurs"}
    if warnings:
        result["warnings"] = warnings
    return result


@mcp.tool()
def search_partner(query: str, limit: int = 10) -> list[dict]:
    """Recherche un client / contact existant dans Odoo par nom."""
    ids = odoo_execute("res.partner", "search", [["name", "ilike", query]], limit=limit)
    if not ids:
        return []
    return odoo_execute(
        "res.partner", "read", ids,
        fields=["id", "name", "email", "phone", "street", "zip", "city"],
    )


@mcp.tool()
def create_partner(
    name: str,
    email: str = "",
    phone: str = "",
    street: str = "",
    zip: str = "",
    city: str = "",
) -> dict:
    """Crée un nouveau contact / client dans Odoo.

    Args:
        name: nom du contact ou de l'entreprise
        email: email optionnel
        phone: téléphone optionnel
        street: adresse (numéro et rue) optionnelle
        zip: code postal optionnel
        city: ville optionnelle
    """
    vals = {"name": name, "email": email, "phone": phone}
    if street:
        vals["street"] = street
    if zip:
        vals["zip"] = zip
    if city:
        vals["city"] = city

    partner_id = odoo_execute("res.partner", "create", vals)
    return {"id": partner_id, "name": name}


def _resolve_variant_id(template_id: int) -> int:
    """Convertit un id de product.template en id de product.product (variante).

    sale.order.line attend une variante, pas un template : search_products et
    create_product travaillent sur product.template, il faut donc résoudre la
    variante correspondante avant de construire une ligne de devis. Si le
    template n'a pas encore de variante indexée (produit tout juste créé),
    on retente une fois après un court délai.
    """
    import time

    for attempt in range(3):
        variant_ids = odoo_execute(
            "product.product", "search",
            [["product_tmpl_id", "=", template_id]], limit=1,
        )
        if variant_ids:
            return variant_ids[0]
        time.sleep(0.5)

    raise ValueError(
        f"Aucune variante (product.product) trouvée pour le produit id {template_id}. "
        "Vérifie que ce produit existe bien et est marqué vendable."
    )


def _build_line_name(variant_id: int, note: str = None) -> str | None:
    """Construit le texte de la ligne de devis : nom du produit + note optionnelle.

    Si note est fourni, renvoie "Nom du produit\\nnote" pour que la note
    apparaisse comme descriptif sous le nom sur le devis. Sinon renvoie None
    pour laisser Odoo utiliser le nom par défaut du produit.
    """
    if not note:
        return None
    product = odoo_execute("product.product", "read", [variant_id], fields=["name"])
    product_name = product[0]["name"] if product else ""
    return f"{product_name}\n{note}" if product_name else note


# --- Heures allouées par devis (champ interne, non visible du client) ---
# Taux horaire de la main d'œuvre, en € HT. Modifiable via la variable
# d'environnement HOURLY_RATE sur Render si le tarif change.
HOURLY_RATE = float(os.environ.get("HOURLY_RATE", "55"))
# Lignes qui comptent dans le calcul (motifs « =ilike », insensibles à la casse) :
# - produits dont le nom commence par « ense »
# - produits « Main d'œuvre » (couvre « d'oeuvre », « d'œuvre » et l'apostrophe typographique)
HOURS_PRODUCT_PATTERNS = ["ense%", "%main d%uvre%"]
HOURS_FIELD = "x_studio_nb_heures"


def _compute_quote_hours(order_id: int) -> float:
    """Calcule les heures allouées d'un devis.

    Pour chaque ligne concernée (produits « ense… » et « Main d'œuvre »), le
    montant HT (price_subtotal) est divisé par le taux horaire et arrondi à la
    demi-heure inférieure ; le total est la somme de ces heures par ligne.
    Ex. avec 55 €/h : ense à 65 € -> 1 h, main d'œuvre à 120 € -> 2 h, total 3 h.
    """
    name_filters = [["product_id.name", "=ilike", pattern] for pattern in HOURS_PRODUCT_PATTERNS]
    # domaine Odoo : order_id = X ET (nom ~ motif1 OU nom ~ motif2)
    domain = [["order_id", "=", order_id]] + ["|"] * (len(name_filters) - 1) + name_filters
    order_lines = odoo_execute(
        "sale.order.line", "search_read", domain, fields=["price_subtotal"],
    )
    # petite marge (1e-9) pour éviter qu'un arrondi flottant fasse perdre une demi-heure
    return sum(
        math.floor(l["price_subtotal"] * 2 / HOURLY_RATE + 1e-9) / 2
        for l in order_lines
    )


def _update_quote_hours(order_id: int) -> float:
    """Recalcule les heures du devis et les enregistre dans x_studio_nb_heures."""
    hours = _compute_quote_hours(order_id)
    odoo_execute("sale.order", "write", [order_id], {HOURS_FIELD: hours})
    return hours


@mcp.tool()
def create_quote(partner_id: int, lines: list[dict], title: str = None) -> dict:
    """Crée un devis (sale.order) dans Odoo pour un client donné.

    Args:
        partner_id: identifiant du client
        lines: liste de dicts, dans l'ordre d'affichage souhaité. Deux types de
            lignes possibles :
            - ligne produit : {product_id, quantity, price_unit (optionnel),
              note (optionnel, texte affiché sous le nom du produit, utile par
              exemple pour détailler une ligne "Main d'œuvre")}
            - ligne de section (titre de lot, ex. "Électricité") :
              {section: "Titre du lot"}
        title: titre du devis (champ x_studio_titre), optionnel

    Les heures allouées (champ interne x_studio_nb_heures) sont calculées
    automatiquement à partir des lignes « ense… » et « Main d'œuvre » (arrondi
    à la demi-heure inférieure ligne par ligne, puis somme).
    """
    order_lines = []
    for line in lines:
        if "section" in line:
            order_lines.append((0, 0, {"display_type": "line_section", "name": line["section"]}))
            continue
        variant_id = _resolve_variant_id(line["product_id"])
        vals = {"product_id": variant_id, "product_uom_qty": line.get("quantity", 1)}
        if "price_unit" in line:
            vals["price_unit"] = line["price_unit"]
        line_name = _build_line_name(variant_id, line.get("note"))
        if line_name:
            vals["name"] = line_name
        order_lines.append((0, 0, vals))

    order_vals = {"partner_id": partner_id, "order_line": order_lines}
    if title:
        order_vals["x_studio_titre"] = title

    order_id = odoo_execute("sale.order", "create", order_vals)
    _update_quote_hours(order_id)
    return odoo_execute(
        "sale.order", "read", [order_id],
        fields=["id", "name", "x_studio_titre", HOURS_FIELD, "amount_total", "state"],
    )[0]


@mcp.tool()
def add_product_to_quote(
    order_id: int,
    product_id: int,
    quantity: float = 1,
    price_unit: float = None,
    note: str = None,
) -> dict:
    """Ajoute une ligne produit à un devis (sale.order) déjà existant.

    Args:
        order_id: identifiant du devis à modifier (le champ 'id' renvoyé par
            create_quote, pas son numéro affiché comme "S00012")
        product_id: identifiant du produit à ajouter (obtenu via search_products
            ou create_product)
        quantity: quantité, 1 par défaut
        price_unit: prix unitaire à surcharger, sinon le prix catalogue est utilisé
        note: texte descriptif optionnel affiché sous le nom du produit sur la
            ligne, utile par exemple pour détailler une ligne "Main d'œuvre"
    """
    variant_id = _resolve_variant_id(product_id)
    vals = {"product_id": variant_id, "product_uom_qty": quantity}
    if price_unit is not None:
        vals["price_unit"] = price_unit
    line_name = _build_line_name(variant_id, note)
    if line_name:
        vals["name"] = line_name

    odoo_execute("sale.order", "write", [order_id], {"order_line": [(0, 0, vals)]})
    _update_quote_hours(order_id)
    return odoo_execute(
        "sale.order", "read", [order_id],
        fields=["id", "name", HOURS_FIELD, "amount_total", "state"],
    )[0]


@mcp.tool()
def recompute_quote_hours(order_id: int) -> dict:
    """Recalcule les heures allouées (x_studio_nb_heures) d'un devis existant.

    À utiliser après avoir modifié les lignes ou les prix directement dans Odoo,
    ou pour renseigner les heures d'un devis créé avant l'ajout de ce champ.
    Règle : pour chaque ligne « ense… » ou « Main d'œuvre », montant HT divisé par
    le taux horaire (55 € par défaut) et arrondi à la demi-heure inférieure ;
    les heures du devis sont la somme de ces lignes.

    Args:
        order_id: identifiant du devis (le champ 'id' renvoyé par create_quote,
            pas son numéro affiché comme "S00012")
    """
    _update_quote_hours(order_id)
    return odoo_execute(
        "sale.order", "read", [order_id],
        fields=["id", "name", HOURS_FIELD, "amount_total", "state"],
    )[0]


@mcp.tool()
def add_quote_section(order_id: int, title: str) -> dict:
    """Ajoute une ligne de section (titre de lot) à un devis existant.

    Utile pour organiser un devis en plusieurs lots, ex. "Électricité",
    "Plomberie", "VMC" — les produits ajoutés ensuite avec add_product_to_quote
    apparaîtront sous la dernière section ajoutée, dans l'ordre du devis.

    Args:
        order_id: identifiant du devis à modifier (le champ 'id' renvoyé par
            create_quote, pas son numéro affiché comme "S00012")
        title: titre de la section (ex. "Électricité")
    """
    vals = {"display_type": "line_section", "name": title}
    odoo_execute("sale.order", "write", [order_id], {"order_line": [(0, 0, vals)]})
    return odoo_execute(
        "sale.order", "read", [order_id],
        fields=["id", "name", "amount_total", "state"],
    )[0]


@mcp.tool()
def set_quote_title(order_id: int, title: str) -> dict:
    """Ajoute ou modifie le titre (champ x_studio_titre) d'un devis existant.

    Args:
        order_id: identifiant du devis à modifier (le champ 'id' renvoyé par
            create_quote, pas son numéro affiché comme "S00012")
        title: le titre à afficher sur le devis
    """
    odoo_execute("sale.order", "write", [order_id], {"x_studio_titre": title})
    return odoo_execute(
        "sale.order", "read", [order_id],
        fields=["id", "name", "x_studio_titre", "amount_total", "state"],
    )[0]


QUOTE_STATE_LABELS = {
    "draft": "Devis",
    "sent": "Devis envoyé",
    "sale": "Bon de commande (confirmé)",
    "done": "Verrouillé",
    "cancel": "Annulé",
}

# Case à cocher (Studio) : cochée quand une commande fournisseur a été générée pour le devis
SUPPLIER_ORDER_FIELD = "x_studio_commande_fournisseur"

QUOTE_SUMMARY_FIELDS = [
    "id", "name", "partner_id", "date_order", "state",
    "x_studio_titre", HOURS_FIELD, SUPPLIER_ORDER_FIELD, "amount_untaxed", "amount_total",
]


def _with_state_label(record: dict) -> dict:
    record["state_label"] = QUOTE_STATE_LABELS.get(record.get("state"), record.get("state"))
    return record


@mcp.tool()
def search_quotes(query: str, limit: int = 10) -> list[dict]:
    """Recherche des devis existants par nom de client, numéro de devis ou titre.

    Exemples de recherche : "BOUFEDJI" (nom du client), "S00012" (numéro du
    devis), "salle de bain" (mot du titre). Les plus récents sont renvoyés en
    premier. Renvoie un résumé de chaque devis (id, numéro, client, date, état,
    titre, heures, montants) ; utilise get_quote avec l'id pour voir le détail
    des lignes.

    Args:
        query: texte à rechercher (client, numéro ou titre)
        limit: nombre maximum de devis renvoyés
    """
    domain = [
        "|", "|",
        ["partner_id.name", "ilike", query],
        ["name", "ilike", query],
        ["x_studio_titre", "ilike", query],
    ]
    orders = odoo_execute(
        "sale.order", "search_read", domain,
        fields=QUOTE_SUMMARY_FIELDS, limit=limit, order="date_order desc, id desc",
    )
    return [_with_state_label(o) for o in orders]


@mcp.tool()
def get_quote(order_id: int = None, number: str = None) -> dict:
    """Lit le détail complet d'un devis : en-tête et toutes les lignes.

    Chaque ligne indique son type (produit, section ou note), le produit, le
    texte affiché, la quantité, le prix unitaire, la remise et les montants HT/TTC.
    Fournir soit le numéro du devis (number), soit son id (order_id).

    Args:
        order_id: identifiant technique du devis (le champ 'id' renvoyé par
            search_quotes ou create_quote)
        number: numéro affiché du devis, tel qu'il apparaît dans Odoo
            (ex. "S00012"), correspondance exacte
    """
    if order_id is None:
        if not number:
            return {"error": "Fournis un numéro de devis (number) ou un id (order_id)."}
        found = odoo_execute(
            "sale.order", "search", [["name", "=ilike", number.strip()]], limit=1,
        )
        if not found:
            return {"error": f"Aucun devis avec le numéro '{number}'. "
                             "Utilise search_quotes pour une recherche approximative."}
        order_id = found[0]

    orders = odoo_execute(
        "sale.order", "read", [order_id],
        fields=QUOTE_SUMMARY_FIELDS + ["amount_tax"],
    )
    if not orders:
        return {"error": f"Aucun devis avec l'id {order_id}."}

    order_lines = odoo_execute(
        "sale.order.line", "search_read",
        [["order_id", "=", order_id]],
        fields=["sequence", "display_type", "product_id", "name", "product_uom_qty",
                "price_unit", "discount", "price_subtotal", "price_total"],
        order="sequence, id",
    )
    for line in order_lines:
        # display_type : False pour une ligne produit, sinon "line_section" ou "line_note"
        line["type"] = line.pop("display_type") or "product"

    result = _with_state_label(orders[0])
    result["lines"] = order_lines
    return result


@mcp.tool()
def delete_quote(order_id: int) -> dict:
    """Supprime définitivement un devis. Irréversible.

    Odoo n'autorise la suppression que des devis en brouillon, envoyés ou annulés.
    Un bon de commande confirmé est refusé : il faut d'abord l'annuler dans Odoo.
    Avant d'appeler cet outil, montre le devis à l'utilisateur (get_quote) et
    demande-lui de confirmer la suppression.

    Args:
        order_id: identifiant du devis à supprimer (le champ 'id' renvoyé par
            search_quotes ou create_quote)
    """
    orders = odoo_execute(
        "sale.order", "read", [order_id],
        fields=["id", "name", "partner_id", "state", "amount_total"],
    )
    if not orders:
        return {"error": f"Aucun devis avec l'id {order_id}."}

    order = orders[0]
    if order["state"] in ("sale", "done"):
        return {
            "error": (
                f"Le devis {order['name']} est confirmé ({QUOTE_STATE_LABELS[order['state']]}) : "
                "il ne peut pas être supprimé. Annule-le d'abord dans Odoo."
            ),
            "id": order_id,
        }

    odoo_execute("sale.order", "unlink", [order_id])
    return {
        "deleted_id": order_id,
        "name": order["name"],
        "partner": order["partner_id"][1] if order["partner_id"] else None,
        "amount_total": order["amount_total"],
    }


def _template_by_ref(reference: str = None, product_id: int = None) -> dict:
    """Retrouve une fiche produit par id ou par référence exacte (la plus ancienne)."""
    if product_id:
        dom = [["id", "=", product_id]]
    elif reference:
        dom = [["default_code", "=", reference]]
    else:
        raise ValueError("product_id ou reference requis")
    res = odoo_execute(
        "product.template", "search_read", dom,
        fields=["id", "name", "default_code", "uom_id"], order="id", limit=1,
    )
    if not res:
        raise ValueError(f"Produit introuvable ({reference or product_id})")
    return res[0]


@mcp.tool()
def create_kit(
    reference: str = None,
    product_id: int = None,
    components: list[dict] = None,
    replace: bool = True,
) -> dict:
    """Crée (ou remplace) la nomenclature de type Kit d'un produit (app Fabrication).

    Args:
        reference: référence interne du produit kit (ex. "ENSEODAPCSIM"). Si plusieurs
            fiches ont la même référence, la plus ancienne est utilisée.
        product_id: alternative à reference (id de la fiche produit)
        components: liste de {reference | product_id, quantity}. La quantité est
            exprimée dans l'unité de mesure de la fiche du composant.
        replace: si True (défaut) et qu'un kit existe déjà, ses composants sont remplacés
    """
    if not components:
        return {"error": "components est vide."}
    kit = _template_by_ref(reference, product_id)

    lines, summary = [], []
    for c in components:
        comp = _template_by_ref(c.get("reference"), c.get("product_id"))
        variant = _resolve_variant_id(comp["id"])
        qty = float(c.get("quantity", 1))
        lines.append((0, 0, {"product_id": variant, "product_qty": qty}))
        summary.append({"reference": comp["default_code"], "name": comp["name"],
                        "quantity": qty, "uom": comp["uom_id"][1] if comp["uom_id"] else None})

    try:
        existing = odoo_execute(
            "mrp.bom", "search",
            [["product_tmpl_id", "=", kit["id"]], ["type", "=", "phantom"],
             ["product_id", "=", False]], limit=1,
        )
    except xmlrpc.client.Fault as e:
        return {"error": "Application Fabrication indisponible ou accès refusé : " + str(e)[:200]}

    if existing:
        if not replace:
            return {"error": f"Un kit existe déjà (bom {existing[0]}). Utilise replace=True."}
        odoo_execute("mrp.bom", "write", existing, {"bom_line_ids": [(5, 0, 0)] + lines})
        bom_id, action = existing[0], "remplacé"
    else:
        bom_id = odoo_execute("mrp.bom", "create", {
            "product_tmpl_id": kit["id"], "type": "phantom",
            "product_qty": 1.0, "bom_line_ids": lines,
        })
        action = "créé"
    return {"bom_id": bom_id, "action": action, "kit": kit["default_code"] or kit["name"],
            "components": summary}


@mcp.tool()
def get_kit(reference: str = None, product_id: int = None) -> dict:
    """Affiche la nomenclature Kit d'un produit (composants et quantités)."""
    kit = _template_by_ref(reference, product_id)
    boms = odoo_execute(
        "mrp.bom", "search_read",
        [["product_tmpl_id", "=", kit["id"]], ["type", "=", "phantom"]],
        fields=["id", "bom_line_ids"], limit=1,
    )
    if not boms:
        return {"kit": kit["default_code"], "components": [], "note": "Pas de kit."}
    lines = odoo_execute(
        "mrp.bom.line", "read", boms[0]["bom_line_ids"],
        fields=["product_id", "product_qty", "product_uom_id"],
    )
    return {"bom_id": boms[0]["id"], "kit": kit["default_code"],
            "components": [{"product": l["product_id"][1], "quantity": l["product_qty"],
                            "uom": l["product_uom_id"][1] if l["product_uom_id"] else None}
                           for l in lines]}


@mcp.tool()
def update_product(
    product_id: int,
    name: str = None,
    price: float = None,
    price_tax_name: str = None,
    cost: float = None,
    cost_tax_name: str = None,
    tag: str = None,
    description: str = None,
    reference: str = None,
) -> dict:
    """Modifie une fiche produit existante dans Odoo.

    Seuls les champs fournis (non None) sont modifiés ; les autres restent inchangés.

    Args:
        reference: nouvelle référence interne (default_code), si à changer
        product_id: identifiant du produit à modifier (obtenu via search_products)
        name: nouveau nom, si à changer
        price: nouveau prix de vente, si à changer
        price_tax_name: nom ou taux de la taxe de VENTE à appliquer sur le prix
            de vente (remplace la taxe de vente existante)
        cost: nouveau prix d'achat, si à changer
        cost_tax_name: nom ou taux de la taxe d'ACHAT à appliquer sur le prix
            d'achat (remplace la taxe d'achat existante)
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
    if reference is not None:
        vals["default_code"] = reference

    warnings = []

    if price_tax_name is not None:
        tax_id = _find_tax(price_tax_name, "sale")
        if tax_id:
            vals["taxes_id"] = [(6, 0, [tax_id])]
        else:
            warnings.append(f"Taxe de vente '{price_tax_name}' introuvable, taxe inchangée.")

    if cost_tax_name is not None:
        tax_id = _find_tax(cost_tax_name, "purchase")
        if tax_id:
            vals["supplier_taxes_id"] = [(6, 0, [tax_id])]
        else:
            warnings.append(f"Taxe d'achat '{cost_tax_name}' introuvable, taxe inchangée.")

    if tag is not None:
        tag_id = _find_or_create_tag(tag)
        vals["product_tag_ids"] = [(6, 0, [tag_id])]

    if not vals:
        return {"error": "Aucun champ à modifier n'a été fourni."}

    odoo_execute("product.template", "write", [product_id], vals)
    updated = odoo_execute(
        "product.template", "read", [product_id],
        fields=["id", "name", "list_price", "standard_price", "default_code",
                 "description_sale", "taxes_id", "supplier_taxes_id", "product_tag_ids"],
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


def _format_quantity(qty: float) -> str:
    """Quantité sans décimale inutile : 5.0 -> '5', 2.5 -> '2.5'."""
    return str(int(qty)) if float(qty).is_integer() else str(round(qty, 3))


MAX_KIT_DEPTH = 3  # un kit peut contenir d'autres kits, jusqu'à 3 niveaux
ENSE_PREFIX = "ense"
KITS_UNAVAILABLE = "__kits_unavailable__"


def _load_variants(variant_ids: list, cache: dict) -> None:
    """Charge dans cache les infos des variantes produit (nom, fiche, référence interne)."""
    todo = [v for v in set(variant_ids) if v not in cache]
    if todo:
        for v in odoo_execute(
            "product.product", "read", todo, fields=["name", "product_tmpl_id", "default_code"],
        ):
            cache[v["id"]] = v


def _load_kits(template_ids: list, cache: dict) -> None:
    """Charge dans cache les nomenclatures de type Kit (mrp.bom « phantom »).

    cache[id_fiche] = [(id_variante_composant, quantité par unité de kit), ...] ;
    une liste vide signifie « pas un kit ». Si l'application Fabrication n'est
    pas installée, cache[KITS_UNAVAILABLE] passe à True et aucun kit n'est développé.
    """
    todo = [t for t in set(template_ids) if t not in cache]
    if not todo:
        return
    for t in todo:
        cache[t] = []
    if cache.get(KITS_UNAVAILABLE):
        return
    try:
        boms = odoo_execute(
            "mrp.bom", "search_read",
            [["type", "=", "phantom"], ["product_tmpl_id", "in", todo], ["product_id", "=", False]],
            fields=["product_tmpl_id", "product_qty", "bom_line_ids"], order="sequence, id",
        )
        for bom in boms:
            tmpl_id = bom["product_tmpl_id"][0]
            if cache[tmpl_id] or not bom["bom_line_ids"]:
                continue  # la première nomenclature (par séquence) l'emporte ; kit vide = ignoré
            bom_lines = odoo_execute(
                "mrp.bom.line", "read", bom["bom_line_ids"], fields=["product_id", "product_qty"],
            )
            factor = bom["product_qty"] or 1.0
            cache[tmpl_id] = [(l["product_id"][0], l["product_qty"] / factor) for l in bom_lines]
    except xmlrpc.client.Fault:
        cache[KITS_UNAVAILABLE] = True


@mcp.tool()
def generate_supplier_order(order_id: int = None, number: str = None, supplier: str = "Yesss") -> dict:
    """Prépare le fichier d'import de panier d'un fournisseur (Yesss par défaut) à partir d'un devis.

    Les produits « kit » (nomenclature de type Kit dans Odoo, par exemple les
    références ENSE) sont remplacés par leurs composants, multipliés par la
    quantité du devis. Seuls les articles associés au fournisseur sont ensuite
    repris : un article est associé s'il porte l'étiquette du fournisseur
    (ex. « Yesss ») ou s'il a ce fournisseur dans sa fiche (onglet Achat). Les
    autres lignes (autres fournisseurs, main d'œuvre, sections, notes) sont
    ignorées et listées dans « skipped ».

    La référence envoyée est le code article du fournisseur (fiche Achat) s'il
    existe, sinon la référence interne de l'article. Les lignes d'un même article
    sont regroupées (quantités additionnées).

    Quand le fichier contient au moins un article, la case « commande fournisseur »
    (x_studio_commande_fournisseur) du devis est cochée automatiquement.

    Le résultat contient « csv », le contenu exact du fichier : une ligne
    « REFERENCE;QUANTITE » par article, sans en-tête, fins de ligne Windows
    (CRLF), comme le modèle d'import. Pour livrer le fichier : l'écrire
    tel quel (en conservant les CRLF) dans « file_name » et l'envoyer à
    l'utilisateur, en lui signalant les lignes de « skipped » qui concernent le
    fournisseur (article associé mais sans référence, ENSE sans kit) et les « warnings ».

    Args:
        order_id: identifiant technique du devis (renvoyé par search_quotes)
        number: numéro affiché du devis (ex. "S00012"), correspondance exacte
        supplier: nom du fournisseur / de l'étiquette, « Yesss » par défaut
    """
    if order_id is None:
        if not number:
            return {"error": "Fournis un numéro de devis (number) ou un id (order_id)."}
        found = odoo_execute(
            "sale.order", "search", [["name", "=ilike", number.strip()]], limit=1,
        )
        if not found:
            return {"error": f"Aucun devis avec le numéro '{number}'."}
        order_id = found[0]

    orders = odoo_execute("sale.order", "read", [order_id], fields=["name", "partner_id"])
    if not orders:
        return {"error": f"Aucun devis avec l'id {order_id}."}
    order = orders[0]

    lines = odoo_execute(
        "sale.order.line", "search_read",
        [["order_id", "=", order_id], ["display_type", "=", False], ["product_id", "!=", False]],
        fields=["product_id", "name", "product_uom_qty"], order="sequence, id",
    )
    if not lines:
        return {"error": f"Le devis {order['name']} ne contient aucune ligne produit."}

    # --- 1) Développement des kits (ex. ENSE) en articles à commander ---
    variants: dict = {}
    kits: dict = {}
    items, kits_expanded, warnings = [], [], []
    _load_variants([l["product_id"][0] for l in lines], variants)
    _load_kits([variants[l["product_id"][0]]["product_tmpl_id"][0] for l in lines], kits)

    def expand(variant_id: int, qty: float, name: str, parent, depth: int) -> None:
        components = kits.get(variants[variant_id]["product_tmpl_id"][0], [])
        if components and depth < MAX_KIT_DEPTH:
            _load_variants([c for c, _ in components], variants)
            _load_kits([variants[c]["product_tmpl_id"][0] for c, _ in components], kits)
            if depth == 0:
                kits_expanded.append({"line": name, "quantity": _format_quantity(qty),
                                      "components": len(components)})
            for comp_variant, comp_qty in components:
                expand(comp_variant, qty * comp_qty, variants[comp_variant]["name"],
                       parent or name, depth + 1)
        else:
            items.append({"variant_id": variant_id, "quantity": qty, "name": name,
                          "parent": parent, "is_kit_line": depth == 0})

    for line in lines:
        expand(line["product_id"][0], line["product_uom_qty"], line["name"].split("\n")[0], None, 0)

    if kits.get(KITS_UNAVAILABLE):
        warnings.append(
            "Nomenclatures (application Fabrication) introuvables dans Odoo : "
            "les kits n'ont pas été développés en composants."
        )

    # --- 2) Association au fournisseur et référence à envoyer ---
    template_ids = list({variants[i["variant_id"]]["product_tmpl_id"][0] for i in items})

    # par étiquette produit
    templates = odoo_execute(
        "product.template", "read", template_ids, fields=["product_tag_ids"],
    )
    all_tag_ids = list({t for tpl in templates for t in tpl["product_tag_ids"]})
    tag_names = {}
    if all_tag_ids:
        tag_names = {t["id"]: t["name"] for t in odoo_execute(
            "product.tag", "read", all_tag_ids, fields=["name"],
        )}
    supplier_lc = supplier.strip().lower()
    tagged_templates = {
        tpl["id"] for tpl in templates
        if any(supplier_lc in str(tag_names.get(t, "")).lower() for t in tpl["product_tag_ids"])
    }

    # par fiche fournisseur (onglet Achat) + code article fournisseur
    supplier_codes: dict = {}
    infos = odoo_execute(
        "product.supplierinfo", "search_read",
        [["product_tmpl_id", "in", template_ids], ["partner_id.name", "ilike", supplier.strip()]],
        fields=["product_tmpl_id", "product_code"],
    )
    for info in infos:
        tmpl_id = info["product_tmpl_id"][0]
        supplier_codes.setdefault(tmpl_id, "")
        if info.get("product_code") and not supplier_codes[tmpl_id]:
            supplier_codes[tmpl_id] = info["product_code"].strip()

    totals: dict[str, dict] = {}
    skipped = []

    def skip(item: dict, reason: str) -> None:
        entry = {"name": item["name"], "reason": reason}
        if item["parent"]:
            entry["kit"] = item["parent"]
        skipped.append(entry)

    for item in items:
        variant = variants[item["variant_id"]]
        tmpl_id = variant["product_tmpl_id"][0]
        qty = item["quantity"]

        if tmpl_id not in tagged_templates and tmpl_id not in supplier_codes:
            if item["is_kit_line"] and variant["name"].lower().startswith(ENSE_PREFIX):
                skip(item, "produit ENSE sans nomenclature Kit dans Odoo (composants inconnus)")
            else:
                skip(item, f"non associé à {supplier}")
            continue

        reference = supplier_codes.get(tmpl_id) or (variant.get("default_code") or "").strip()
        if not reference:
            skip(item, f"associé à {supplier} mais sans référence")
            continue
        if qty <= 0:
            skip(item, "quantité nulle")
            continue
        if not float(qty).is_integer():
            warnings.append(f"Quantité non entière pour {reference} : {_format_quantity(qty)}")

        entry = totals.setdefault(
            reference, {"reference": reference, "name": item["name"], "quantity": 0.0, "kits": []},
        )
        entry["quantity"] += qty
        if item["parent"] and item["parent"] not in entry["kits"]:
            entry["kits"].append(item["parent"])

    included = list(totals.values())
    ticked = False
    if included:
        try:
            odoo_execute("sale.order", "write", [order_id], {SUPPLIER_ORDER_FIELD: True})
            ticked = True
        except xmlrpc.client.Fault as err:
            warnings.append(
                f"Fichier généré, mais la case « {SUPPLIER_ORDER_FIELD} » n'a pas pu être cochée : "
                f"{str(err)[:200]}"
            )
    csv_text = "".join(f"{e['reference']};{_format_quantity(e['quantity'])}\r\n" for e in included)
    for e in included:
        e["quantity"] = _format_quantity(e["quantity"])
        if not e["kits"]:
            del e["kits"]

    return {
        "quote": order["name"],
        "client": order["partner_id"][1] if order["partner_id"] else None,
        "supplier": supplier,
        "file_name": f"commande_{supplier.strip().lower()}_{order['name']}.csv",
        "csv": csv_text,
        "commande_fournisseur_cochee": ticked,
        "lines_included": included,
        "kits_expanded": kits_expanded,
        "skipped": skipped,
        "warnings": warnings,
    }


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
