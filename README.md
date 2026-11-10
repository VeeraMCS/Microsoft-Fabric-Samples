# Dynamic Power BI RLS Driven by an Entitlements Table (Microsoft Fabric)

Managers see their team. Users see only the product categories they're entitled to. One Microsoft
Fabric notebook keeps it current from your access-management system.

The semantic model has **one RLS role** with **two dynamic rules** that combine with AND:

| Rule | Question | Data it uses | Who maintains it |
|---|---|---|---|
| 1. On **DimEmployee** | **Who**: whose rows can I see? | Manager hierarchy (`EmployeePath`) | HR load (as usual) |
| 2. On **DimProduct** | **What**: which product categories? | `SecurityUserCategory`, built from the **entitlements table** | **This notebook** |

The notebook:

1. **Reads** the entitlements (user → category, with start and end dates) and works out what is active or expired.
2. **Rewrites** the `SecurityUserCategory` Delta table from the active entitlements.
3. **Keeps the role's members in sync**: a user's first active entitlement adds them; when their last one ends, they're removed. (Optional: use one Entra group instead.)
4. **Saves** member changes over XMLA, **refreshes** the security table in the model, and **logs** every change.

```
Entitlements table ──► Fabric notebook ──► SecurityUserCategory (Delta) ──► refresh
                                    └──────► "Sales Access" role members (XMLA)
                                    └──────► Audit table (Delta)
HR load ──► DimEmployee (with EmployeePath)   ← not touched by this notebook
```

---

## Contents

| File | Purpose |
|---|---|
| `rls_role_sync.py` | The notebook code. Paste it into a Fabric notebook. |
| `README.md` | This setup guide. |
| `images/` | An illustrated Fabric UI walkthrough. |

---

## The worked example

### DimEmployee (from HR)

| EmployeeKey | Name | Email | ManagerKey | EmployeePath |
|---:|---|---|---:|---|
| 1 | Asha | asha@contoso.com | | 1 |
| 2 | Ben | ben@contoso.com | 1 | 1\|2 |
| 3 | Chen | chen@contoso.com | 1 | 1\|3 |
| 4 | Dana | dana@contoso.com | 2 | 1\|2\|4 |
| 5 | Eli | eli@contoso.com | 2 | 1\|2\|5 |
| 6 | Farah | farah@contoso.com | 3 | 1\|3\|6 |

Asha (Sales Director) → Ben (→ Dana, Eli) and Chen (→ Farah).

### DimProduct and FactSales

| Product | CategoryKey | Category |
|---|---:|---|
| Laptops | 1 | Computers |
| Monitors | 2 | Displays |
| Docks | 3 | Accessories |

| Employee | Product | Amount |
|---|---|---:|
| Ben | Laptops | 20,000 |
| Ben | Monitors | 8,000 |
| Dana | Laptops | 15,000 |
| Dana | Docks | 3,000 |
| Eli | Laptops | 12,000 |
| Eli | Monitors | 6,000 |
| Chen | Laptops | 18,000 |
| Chen | Docks | 4,000 |
| Farah | Laptops | 9,000 |
| Farah | Monitors | 7,000 |

Total 102,000. FactSales relates to DimEmployee and DimProduct (one-to-many).

### The entitlements table (source)

As of **10 Nov 2026** (`UPN_DOMAIN = "contoso.com"`):

| User | Category | StartDate | EndDate | TransactionId | Today |
|---|---|---|---|---|---|
| asha | All Categories | 2025-06-01 | | T-1001 | Active → **keep** |
| ben | Computers | 2026-01-15 | | T-1002 | Active → **keep** |
| ben | Displays | 2026-03-01 | 2026-10-31 | T-1003 | Expired → **row removed** (ben stays in the role) |
| chen | Computers | 2026-02-01 | | T-1004 | Active → **keep** |
| chen | Accessories | 2026-02-01 | | T-1005 | Active → **keep** |
| dana | Computers | 2026-12-01 | | T-1006 | Starts later → **wait** |
| eli | Displays | 2026-11-05 | | T-1007 | New → **row added, added to the role** |
| farah | Accessories | 2025-09-01 | 2026-10-15 | T-1008 | Expired, her last one → **row removed, removed from the role** |

The hierarchy is **not** in this table. It comes from HR.

### The role (set up once by the model owner)

Role **Sales Access**, two table filters:

**Rule 1: who (DimEmployee)**

```DAX
VAR me =
    LOOKUPVALUE ( DimEmployee[EmployeeKey],
        DimEmployee[Email], USERPRINCIPALNAME () )
RETURN
    NOT ISBLANK ( me )
        && PATHCONTAINS ( DimEmployee[EmployeePath], me )
```

Ben (key 2) matches the paths `1|2`, `1|2|4` and `1|2|5`, so he sees Ben, Dana and Eli.

**Rule 2: what (DimProduct)**

```DAX
DimProduct[CategoryKey] IN
    CALCULATETABLE (
        VALUES ( SecurityUserCategory[CategoryKey] ),
        SecurityUserCategory[UserEmail] = USERPRINCIPALNAME ()
    )
```

- Filters on two tables **combine with AND**, and both flow to FactSales through the relationships.
- `SecurityUserCategory` is **hidden** and has **no relationships**.
- No matching rows means no data. The pattern **fails closed**.

### After one run

**SecurityUserCategory** (8 rows → 7 rows)

| UserEmail | CategoryKey |
|---|---:|
| asha@contoso.com | 1 (Computers) |
| asha@contoso.com | 2 (Displays) |
| asha@contoso.com | 3 (Accessories) |
| ben@contoso.com | 1 (Computers) |
| chen@contoso.com | 1 (Computers) |
| chen@contoso.com | 3 (Accessories) |
| eli@contoso.com | 2 (Displays) |

Removed: ben · Displays, farah · Accessories. Added: eli · Displays.

**Sales Access members:** before asha, ben, chen, farah → after asha, ben, chen, eli.

> **Demo note:** in this tiny example, 2 of 8 security rows (25%) and 1 of 4 members (25%) are removed,
> which trips the default 10% safety stops. Set `MAX_SECURITY_SHRINK_PCT = 0.3` and
> `MAX_REMOVE_PCT = 0.3` for the demo, and keep them low in production.

### What each user sees

| User | Team (rule 1) | Categories (rule 2) | Total |
|---|---|---|---:|
| asha | Everyone | All | 102,000 |
| ben | Ben, Dana, Eli | Computers | 47,000 (61,000 before this run) |
| chen | Chen, Farah | Computers, Accessories | 31,000 |
| eli | Eli | Displays | 6,000 |
| dana | — | None until 1 Dec | Nothing |
| farah | — | Ended | Nothing |

RLS applies to **Viewers**. Workspace Admins, Members and Contributors see all data.

### Something changed: where does it go?

| Change | Update | Role member change? |
|---|---|---|
| New hire joins Ben's team | HR → DimEmployee | None. Ben sees them after the next refresh |
| Employee moves to another manager | HR → DimEmployee | None |
| User gets a category | Entitlements table | Added to the role if it's their first |
| A category entitlement ends | Entitlements table | Removed from the role if it was their last |
| New product category | DimProduct | None |
| Change the rules themselves | Role DAX | Model owner (rare) |

Nobody edits role members by hand. **HR data drives who. Entitlements drive what.**

### Why the role and the table both matter

A Viewer needs all three layers:

| Layer | If it's missing |
|---|---|
| Access to the report (Viewer, app audience or share) | Can't open the report |
| Membership of **Sales Access** | Visuals show a permission error |
| A security row (and their own employee row) | Visuals are blank |

- `SYNC_ROLE_MEMBERS = True` (default): the notebook manages membership from the entitlements.
  Removal applies as soon as it's saved.
- `SYNC_ROLE_MEMBERS = False`: add one Entra security group (for example, the report audience) to the
  role once. The data alone decides what each person sees.

---

## 1. Prerequisites

| Area | Requirement | Who sets it |
|---|---|---|
| Tenant | **Allow XMLA endpoints** enabled | Fabric admin |
| Tenant | **Service principals can use Fabric APIs** (if you use one), limited to a security group | Fabric admin |
| Capacity | **XMLA Endpoint = Read Write** | Capacity admin |
| Workspace | Run-as identity has **Contributor** (or higher): writes Lakehouse tables and role members, and refreshes the model | Workspace admin |
| Model | `SecurityUserCategory` table and the **Sales Access** role with both filters (see above) | Model owner |
| HR data | `DimEmployee.Email` = each person's **sign-in UPN**, one row per person | HR / data owner |

> Viewer, or Read/Build on the model alone, is **not** enough to write role members over XMLA.

## 2. The employee path (model setup, outside this script)

Rule 1 needs an `EmployeePath` column on DimEmployee. The notebook doesn't build or change it.

- **Import model:** add a calculated column `EmployeePath = PATH ( DimEmployee[EmployeeKey], DimEmployee[ManagerKey] )`.
- **Direct Lake model:** calculated columns aren't supported on Direct Lake tables, so add `EmployeePath`
  (for example `1|2|4`) in your HR load into the Lakehouse.

## 3. Your entitlements table

| Logical field | Default column | Required | Notes |
|---|---|---|---|
| `user` | `User` | Yes | Full UPN/email **or** just the alias (the domain is appended) |
| `value` | `Category` | Yes | A category name in `SCOPE_TABLE`, or `ALL_VALUE` |
| `start` | `StartDate` | Yes | Date, timestamp or text |
| `end` | `EndDate` | Yes | Empty = open-ended |
| `app` | `ApplicationCode` | No | Filter rows to one application |
| `access` | `AccessFlag` | No | Filter on an access flag (for example `Y`) |
| `delegation` | `OnBehalfOf` | No | Include or exclude delegated rows |
| `txn_id` | `TransactionId` | No | Written to the audit table |
| `modified_on` | `ModifiedOn` | No | Picks the latest row per user and category |

Map your column names in `COL`. Set an optional column to `None` if you don't have it.

- **Active** = `StartDate` ≤ today **and** (`EndDate` empty **or** ≥ today)
- **Expired** = `EndDate` < today
- A user stays in the role while **any** entitlement is active. Users with no source rows are never removed.

## 4. Install the library once (Fabric Environment)

1. Workspace → **+ New item → Environment** (for example `env-rls-sync`).
2. **Libraries → Public libraries → Add from PyPI** → `semantic-link-labs`, **pinned** to the version you tested.
3. **Save → Publish**.
4. Attach it to the notebook, or make it the **workspace default**
   (*Workspace settings → Data Engineering/Science → Spark settings → Environment*; needs Admin).
5. Restart the session and check: `import importlib.metadata as md; print(md.version("semantic-link-labs"))`.

## 5. Create the notebook and set parameters

Paste `rls_role_sync.py` into a Fabric notebook. Put the **Parameters** block in its own cell and mark it
**Toggle parameter cell**.

| Parameter | What to set |
|---|---|
| `WORKSPACE`, `MODEL` | Workspace and semantic model name or id |
| `SOURCE_TABLE`, `AUDIT_TABLE` | `<lakehouse>.<table>` |
| `APPLICATION_CODE`, `ACCESS_ALLOWED`, `INCLUDE_DELEGATION` | Source filters, or `None` / `True` |
| `UPN_DOMAIN`, `EQUIVALENT_DOMAINS` | Domain appended to aliases; domains that mean the same person |
| `SCOPE_TABLE`, `SCOPE_NAME_COL`, `SCOPE_KEY_COL`, `ALL_VALUE` | Category lookup (`dim_product`), its name and key columns, and the "all" value |
| `SECURITY_TABLE`, `SECURITY_USER_COL`, `MODEL_SECURITY_TABLE` | Lakehouse table written, its user column, and its name in the model |
| `RLS_ROLE`, `SYNC_ROLE_MEMBERS` | The role name; `False` if a group is its member |
| `REFRESH_MODEL` | Refresh or reframe the security table after writing |
| `MAX_REMOVE_PCT`, `MAX_SECURITY_SHRINK_PCT` | Safety stops (default 10%) |
| `DRY_RUN` | `True` to report only |

## 6. Test safely

1. Run with `DRY_RUN = True`. Check the printed role names, the security-table change (`old -> new rows`),
   and the members to add or remove.
2. Fix any `unmapped` values (add the category to `dim_product`, or correct the source).
3. Apply on a test model with `DRY_RUN = False`.
4. In Power BI: semantic model → **Security** → **Test as role** → tick **Other user** → enter a UPN.

## 7. Step by step in the Fabric UI

> The screens are simplified illustrations of the Fabric UI. Labels can vary by release.

**1. Find the model's security settings.** **⋯ → Security** for members; **Open data model → Manage roles** for filters.

<img width="614" height="339" alt="image" src="https://github.com/user-attachments/assets/2ce7e240-ea83-430b-982c-382dda09a776" />


**2. Rule 1 on DimEmployee.**

<img width="616" height="357" alt="image" src="https://github.com/user-attachments/assets/3d38c9b9-a9d5-4799-8ee2-a2d4b715c278" />

**3. Rule 2 on DimProduct.**

<img width="621" height="361" alt="image" src="https://github.com/user-attachments/assets/3f076782-ce7b-4521-ae03-1b84d798bc94" />



**4. Members are written for you.** eli was added on his first entitlement; farah was removed when her last one ended.

<img width="620" height="370" alt="image" src="https://github.com/user-attachments/assets/2397dfb2-399e-4fa5-8e66-392b0343c5cf" />


**5. Test as role + other user.** ben sees his team in Computers only: 47,000.

<img width="622" height="338" alt="image" src="https://github.com/user-attachments/assets/90fd9082-de8a-43f4-b3af-00fe3d0883ac" />


**6. Check the audit trail.**

```sql
SELECT run_ts, action, role_name, member, scope_key, source_txn_id, status
FROM rls_role_sync_audit
ORDER BY run_ts DESC;
```

Actions: `SEC_ADD`, `SEC_REMOVE`, `ADD`, `REMOVE`, `REFRESH`, `UNMAPPED_VALUE`.
Status: `DRY_RUN`, `APPLIED`, `FAILED` (with `error`), `SKIPPED`, `LOGGED`.

![Audit table results](images/06-audit.png)

**7. Schedule with a service principal.**

![Pipeline Notebook activity with a service principal connection](images/07-pipeline.png)

## 8. Schedule it (and choose the run-as identity)

| Trigger | Runs as |
|---|---|
| Manual run | The person clicking Run |
| Notebook schedule | Whoever created or last updated the schedule |
| Pipeline Notebook activity (default) | Whoever last modified the pipeline |
| Pipeline Notebook activity **with a connection** | The **service principal** or **workspace identity** ✅ |

Recommended: a pipeline Notebook activity with a **service principal** connection (secret in Azure Key
Vault, Contributor on the workspace). Pass `DRY_RUN` as a base parameter; `"False"` as text is parsed
correctly. Run it after the entitlements and HR loads.

## Guardrails built in

- **Dry-run mode**: see exactly what would change before anything is written.
- **Safety stops**: abort if more than `MAX_REMOVE_PCT` of members or `MAX_SECURITY_SHRINK_PCT` of security rows would be removed, or if the security table would be empty.
- **Exact names**: the role is matched ignoring case and spaces; members are removed using the exact stored name.
- **Audit trail**: every change is logged with the run id and the source transaction id.
- **Fails loudly**: a failed member save or refresh fails the run, so the pipeline alerts.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Output only shows `DRY_RUN` | `DRY_RUN` still `True` | Set `DRY_RUN = False` (base parameter `false`) |
| User in the role sees blank visuals | No active category row, or `DimEmployee.Email` ≠ their UPN | Check both; use **Test as role → Other user** |
| Manager sees only their own rows | `EmployeePath` missing or stale | Fix it in the HR load or model; refresh DimEmployee |
| Rule 1 error "multiple values" | Duplicate emails in DimEmployee | One row per person |
| Changes not visible | Security table not refreshed or reframed | Keep `REFRESH_MODEL = True` |
| Report falls back to DirectQuery | Security table is a view | Write a Delta table |
| Everyone appears in "add" | Members stored with a different domain | Add it to `EQUIVALENT_DOMAINS` |
| Permission or XMLA error | XMLA not Read Write, or identity lacks Contributor | See Prerequisites |
| `ModuleNotFoundError: sempy_labs` | Environment not attached or published | See step 4 |

## Things to know

- **Guests (B2B):** `USERPRINCIPALNAME()` may not equal their email. Test with **Other user**.
- **PBIX download:** after a model is changed over XMLA it usually can't be downloaded as a `.pbix`.
- **Deployments:** if a deployment includes role members, it can overwrite synced members. Keep members out of source control, or re-run after each deployment.

## License

MIT. Use at your own risk. Test with `DRY_RUN = True` on a non-production model first.
