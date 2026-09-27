import unittest
from types import SimpleNamespace

from sign402_gateway import web_internal

PRODUCTS = [
    {"productId": "thuisbezorgd-netherlands", "name": "Thuisbezorgd", "country": "NL", "category": "food",
     "categories": ["food", "food-delivery"], "productType": "gift_card", "recipientType": "none", "inStock": True},
    {"productId": "esim-netherlands", "name": "Netherlands eSIM", "country": "NL", "category": "data",
     "categories": ["data"], "productType": "esim", "recipientType": "none", "inStock": True},
    {"productId": "kpn-netherlands", "name": "KPN", "country": "NL", "category": "refill",
     "categories": ["refill"], "productType": "phone_refill", "recipientType": "phone_number", "inStock": True},
    {"productId": "gone-netherlands", "name": "Gone", "country": "NL", "category": "food",
     "categories": ["food"], "productType": "gift_card", "recipientType": "none", "inStock": False},
]


class FakeCatalog:
    def __init__(self):
        self.calls = []

    def search_products(self, **kwargs):
        self.calls.append(("search", kwargs))
        return [p for p in PRODUCTS if kwargs["query"].lower() in p["name"].lower()
                and (not kwargs["product_type"] or p["productType"] == kwargs["product_type"])]

    def list_products(self, **kwargs):
        self.calls.append(("list", kwargs))
        wanted = set(filter(None, kwargs["category"].split(",")))
        return [p for p in PRODUCTS if not wanted or wanted & set(p["categories"])]


class CatalogSearchTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FakeCatalog()
        self.server = SimpleNamespace(bitrefill_search_service=SimpleNamespace(bitrefill_client=self.catalog))

    def search(self, **payload):
        return web_internal.catalog_search(self.server, payload)[1]

    def test_a_countrys_shops_of_a_kind_without_a_brand(self):
        found = self.search(query="", country="nl", category="food", productType="gift_card")
        self.assertEqual([p["slug"] for p in found["products"]], ["thuisbezorgd-netherlands"])  # in stock only
        self.assertEqual(self.catalog.calls[0][1]["category"], "food,restaurants,food-delivery,groceries")
        self.assertEqual(found["products"][0]["categories"], ["food", "food-delivery"])

    def test_words_search_the_catalog_by_type_and_phone_products_are_marked(self):
        esims = self.search(query="netherlands", country="NL", category="", productType="esim")["products"]
        self.assertEqual([p["slug"] for p in esims], ["esim-netherlands"])
        refills = self.search(query="kpn", country="NL", category="", productType="phone_refill")["products"]
        self.assertTrue(refills[0]["needsRecipient"])

    def test_nothing_to_search_or_no_catalog(self):
        self.assertEqual(self.search(query="", country="", productType="gift_card")["products"], [])
        self.server.bitrefill_search_service = None
        self.assertEqual(self.search(query="steam")["error"], "catalog_off")


if __name__ == "__main__":
    unittest.main()
