"""Wave Accounting public GraphQL API client."""

from __future__ import annotations

from typing import Any

from ledgerdemain.http_client import HttpClient
from ledgerdemain.models import (
    WaveAccount,
    WaveCustomer,
    WaveProduct,
)
from ledgerdemain.money import ZERO, parse_money


class WaveApiError(RuntimeError):
    """Raised when a Wave GraphQL mutation returns didSucceed=false / inputErrors."""

    def __init__(self, operation: str, input_errors: list[dict[str, Any]]) -> None:
        formatted = "; ".join(
            f"[{err.get('code', 'ERROR')}] {'.'.join(str(p) for p in err.get('path', []))}: "
            f"{err.get('message', '')}"
            for err in input_errors
        )
        super().__init__(f"Wave {operation} failed: {formatted or 'unknown input error'}")
        self.operation = operation
        self.input_errors = input_errors


_BUSINESS_QUERY = """
query GetBusiness($businessId: ID!) {
  business(id: $businessId) {
    id
    name
    isClassicAccounting
    currency {
      code
    }
  }
}
"""

_LIST_BUSINESSES_QUERY = """
query ListBusinesses($page: Int!, $pageSize: Int!) {
  businesses(page: $page, pageSize: $pageSize) {
    pageInfo {
      currentPage
      totalPages
      totalCount
    }
    edges {
      node {
        id
        name
        isClassicAccounting
        currency {
          code
        }
      }
    }
  }
}
"""

_LIST_ACCOUNTS_QUERY = """
query ListAccounts($businessId: ID!, $page: Int!, $pageSize: Int!, $isArchived: Boolean) {
  business(id: $businessId) {
    id
    accounts(page: $page, pageSize: $pageSize, isArchived: $isArchived) {
      pageInfo {
        currentPage
        totalPages
        totalCount
      }
      edges {
        node {
          id
          name
          description
          type {
            name
            value
            normalBalanceType
          }
          subtype {
            name
            value
          }
          isArchived
        }
      }
    }
  }
}
"""

_LIST_CUSTOMERS_QUERY = """
query ListCustomers($businessId: ID!, $page: Int!, $pageSize: Int!) {
  business(id: $businessId) {
    id
    customers(page: $page, pageSize: $pageSize) {
      pageInfo {
        currentPage
        totalPages
        totalCount
      }
      edges {
        node {
          id
          name
          email
        }
      }
    }
  }
}
"""

_LIST_PRODUCTS_QUERY = """
query ListProducts($businessId: ID!, $page: Int!, $pageSize: Int!) {
  business(id: $businessId) {
    id
    products(page: $page, pageSize: $pageSize) {
      pageInfo {
        currentPage
        totalPages
        totalCount
      }
      edges {
        node {
          id
          name
          unitPrice
          incomeAccount {
            id
            name
          }
        }
      }
    }
  }
}
"""

_CUSTOMER_CREATE_MUTATION = """
mutation CustomerCreate($input: CustomerCreateInput!) {
  customerCreate(input: $input) {
    didSucceed
    inputErrors {
      path
      message
      code
    }
    customer {
      id
      name
      email
    }
  }
}
"""

_PRODUCT_CREATE_MUTATION = """
mutation ProductCreate($input: ProductCreateInput!) {
  productCreate(input: $input) {
    didSucceed
    inputErrors {
      path
      message
      code
    }
    product {
      id
      name
      unitPrice
      incomeAccount {
        id
        name
      }
    }
  }
}
"""

_INVOICE_CREATE_MUTATION = """
mutation InvoiceCreate($input: InvoiceCreateInput!) {
  invoiceCreate(input: $input) {
    didSucceed
    inputErrors {
      path
      message
      code
    }
    invoice {
      id
      invoiceNumber
      invoiceDate
      dueDate
      status
      total {
        value
      }
      customer {
        id
        name
      }
    }
  }
}
"""

_MONEY_TX_CREATE_MUTATION = """
mutation MoneyTransactionCreate($input: MoneyTransactionCreateInput!) {
  moneyTransactionCreate(input: $input) {
    didSucceed
    inputErrors {
      path
      message
      code
    }
    transaction {
      id
    }
  }
}
"""


class WaveGraphQLClient:
    """Client for Wave Accounting's GraphQL API (`https://gql.waveapps.com/graphql/public`)."""

    def __init__(
        self,
        endpoint: str,
        access_token: str,
        http_client: HttpClient | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.access_token = access_token
        self.http = http_client or HttpClient()

    def _gql(
        self, query: str, variables: dict[str, Any] | None = None, *, idempotent: bool = True
    ) -> dict[str, Any]:
        return self.http.post_graphql(
            endpoint=self.endpoint,
            access_token=self.access_token,
            query=query,
            variables=variables,
            idempotent=idempotent,
        )

    def get_business(self, business_id: str) -> dict[str, Any]:
        """Fetch business metadata including isClassicAccounting."""
        data = self._gql(_BUSINESS_QUERY, {"businessId": business_id})
        return dict(data.get("business") or {})

    def list_businesses(self, page_size: int = 50) -> list[dict[str, Any]]:
        """List accessible Wave businesses."""
        nodes: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._gql(_LIST_BUSINESSES_QUERY, {"page": page, "pageSize": page_size})
            conn = data.get("businesses") or {}
            for edge in conn.get("edges") or []:
                if edge.get("node"):
                    nodes.append(dict(edge["node"]))
            info = conn.get("pageInfo") or {}
            if page >= int(info.get("totalPages", 1)):
                break
            page += 1
        return nodes

    def list_accounts(
        self, business_id: str, is_archived: bool | None = False, page_size: int = 100
    ) -> list[WaveAccount]:
        """List accounts in the business Chart of Accounts."""
        accounts: list[WaveAccount] = []
        page = 1
        while True:
            data = self._gql(
                _LIST_ACCOUNTS_QUERY,
                {
                    "businessId": business_id,
                    "page": page,
                    "pageSize": page_size,
                    "isArchived": is_archived,
                },
            )
            conn = (data.get("business") or {}).get("accounts") or {}
            for edge in conn.get("edges") or []:
                node = edge.get("node") or {}
                acc_type = node.get("type") or {}
                subtype = node.get("subtype") or {}
                accounts.append(
                    WaveAccount(
                        wave_id=str(node["id"]),
                        name=str(node["name"]),
                        account_type=str(acc_type.get("value") or acc_type.get("name") or ""),
                        account_subtype=str(subtype.get("value") or subtype.get("name") or ""),
                        normal_balance_type=str(acc_type.get("normalBalanceType") or "DEBIT"),
                        is_archived=bool(node.get("isArchived", False)),
                        description=str(node.get("description") or ""),
                    )
                )
            info = conn.get("pageInfo") or {}
            if page >= int(info.get("totalPages", 1)):
                break
            page += 1
        return accounts

    def list_customers(self, business_id: str, page_size: int = 100) -> list[WaveCustomer]:
        """List all customers in a Wave business."""
        customers: list[WaveCustomer] = []
        page = 1
        while True:
            data = self._gql(
                _LIST_CUSTOMERS_QUERY,
                {"businessId": business_id, "page": page, "pageSize": page_size},
            )
            conn = (data.get("business") or {}).get("customers") or {}
            for edge in conn.get("edges") or []:
                node = edge.get("node") or {}
                customers.append(
                    WaveCustomer(
                        wave_id=str(node["id"]),
                        name=str(node["name"]),
                        email=str(node.get("email") or ""),
                    )
                )
            info = conn.get("pageInfo") or {}
            if page >= int(info.get("totalPages", 1)):
                break
            page += 1
        return customers

    def list_products(self, business_id: str, page_size: int = 100) -> list[WaveProduct]:
        """List all products/services in a Wave business."""
        products: list[WaveProduct] = []
        page = 1
        while True:
            data = self._gql(
                _LIST_PRODUCTS_QUERY,
                {"businessId": business_id, "page": page, "pageSize": page_size},
            )
            conn = (data.get("business") or {}).get("products") or {}
            for edge in conn.get("edges") or []:
                node = edge.get("node") or {}
                income_acc = node.get("incomeAccount") or {}
                products.append(
                    WaveProduct(
                        wave_id=str(node["id"]),
                        name=str(node["name"]),
                        unit_price=parse_money(node.get("unitPrice"), ZERO),
                        income_account_id=str(income_acc.get("id") or ""),
                    )
                )
            info = conn.get("pageInfo") or {}
            if page >= int(info.get("totalPages", 1)):
                break
            page += 1
        return products

    def create_customer(
        self, business_id: str, name: str, email: str | None = None
    ) -> WaveCustomer:
        """Create a customer via customerCreate mutation."""
        payload: dict[str, Any] = {"businessId": business_id, "name": name}
        if email:
            payload["email"] = email
        data = self._gql(_CUSTOMER_CREATE_MUTATION, {"input": payload}, idempotent=False)
        res = data.get("customerCreate") or {}
        if not res.get("didSucceed"):
            raise WaveApiError("customerCreate", res.get("inputErrors") or [])
        node = res["customer"]
        return WaveCustomer(
            wave_id=str(node["id"]),
            name=str(node["name"]),
            email=str(node.get("email") or ""),
        )

    def create_product(
        self,
        business_id: str,
        name: str,
        income_account_id: str,
        unit_price: str = "0.00",
    ) -> WaveProduct:
        """Create a product/service via productCreate mutation."""
        payload = {
            "businessId": business_id,
            "name": name,
            "unitPrice": unit_price,
            "incomeAccountId": income_account_id,
        }
        data = self._gql(_PRODUCT_CREATE_MUTATION, {"input": payload}, idempotent=False)
        res = data.get("productCreate") or {}
        if not res.get("didSucceed"):
            raise WaveApiError("productCreate", res.get("inputErrors") or [])
        node = res["product"]
        income_acc = node.get("incomeAccount") or {}
        return WaveProduct(
            wave_id=str(node["id"]),
            name=str(node["name"]),
            unit_price=parse_money(node.get("unitPrice"), ZERO),
            income_account_id=str(income_acc.get("id") or income_account_id),
        )

    def create_invoice(
        self,
        business_id: str,
        customer_id: str,
        invoice_number: str,
        invoice_date: str,
        due_date: str,
        items: list[dict[str, Any]],
        status: str = "SAVED",
    ) -> dict[str, Any]:
        """Create an invoice via invoiceCreate mutation."""
        payload = {
            "businessId": business_id,
            "customerId": customer_id,
            "invoiceNumber": invoice_number,
            "invoiceDate": invoice_date,
            "dueDate": due_date,
            "status": status,
            "items": items,
        }
        data = self._gql(_INVOICE_CREATE_MUTATION, {"input": payload}, idempotent=False)
        res = data.get("invoiceCreate") or {}
        if not res.get("didSucceed"):
            raise WaveApiError("invoiceCreate", res.get("inputErrors") or [])
        return dict(res["invoice"])

    def create_money_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a money transaction via moneyTransactionCreate mutation."""
        data = self._gql(_MONEY_TX_CREATE_MUTATION, {"input": payload}, idempotent=False)
        res = data.get("moneyTransactionCreate") or {}
        if not res.get("didSucceed"):
            raise WaveApiError("moneyTransactionCreate", res.get("inputErrors") or [])
        return dict(res["transaction"])
