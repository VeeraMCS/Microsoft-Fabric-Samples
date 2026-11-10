# =====================================================================
# Dynamic Power BI RLS driven by an entitlements table
# (product-category entitlements)
# ---------------------------------------------------------------------
# Runs in a Microsoft Fabric notebook (PySpark).
#
# The semantic model has ONE RLS role (for example "Sales Access") whose
# DAX filter on DimProduct keeps only the product categories listed in
# SecurityUserCategory for the signed-in user (USERPRINCIPALNAME()).
# Any other filters on the role are left untouched.
#
# This notebook:
#   1. Reads the entitlements (user -> category, with start/end dates)
#      and works out what is active and what has expired.
#   2. Rewrites the security table (UserEmail, CategoryKey) from the
#      active entitlements. "All Categories" expands to every category.
#   3. Keeps the one role's members in sync: anyone with an active
#      entitlement is in; anyone with only expired ones is out.
#      (Set SYNC_ROLE_MEMBERS = False if one Entra group is the member.)
#   4. Saves member changes over XMLA, then refreshes the security table
#      in the model so new rows take effect.
# Every change is written to a Delta audit table.
#
# Prerequisite: semantic-link-labs in the attached (or workspace default)
# Fabric Environment. See README.md for the full setup guide.
# License: MIT
# =====================================================================
import uuid
from datetime import datetime, timezone
from collections import defaultdict
from pyspark.sql import functions as F, Window
from pyspark.sql.types import StringType, DateType, TimestampType
from sempy_labs.tom import connect_semantic_model

# ---------------- Parameters (mark this cell as a parameter cell) ----------------
WORKSPACE          = "<Fabric workspace name or id>"
MODEL              = "<Semantic model name or id>"
SOURCE_TABLE       = "<lakehouse>.<entitlements_table>"
AUDIT_TABLE        = "<lakehouse>.rls_role_sync_audit"
APPLICATION_CODE   = None            # value to filter on in COL["app"]; None = use all rows
UPN_DOMAIN         = "contoso.com"   # appended when the user column holds only the alias
EQUIVALENT_DOMAINS = ["contoso.com"] # domains treated as the same person when comparing
ACCESS_ALLOWED     = None            # e.g. ["Y", "Yes", "1"]; None = don't filter on COL["access"]
INCLUDE_DELEGATION = True            # False = ignore rows where COL["delegation"] is populated
DRY_RUN            = True            # True = report only, False = apply changes

# Category scope (lookup used to turn an entitlement value into a key)
SCOPE_TABLE        = "<lakehouse>.dim_product"          # any table with category name + key
SCOPE_NAME_COL     = "Category"
SCOPE_KEY_COL      = "CategoryKey"
ALL_VALUE          = "All Categories"                   # entitlement value meaning "every category"

# Security table and role
SECURITY_TABLE       = "<lakehouse>.security_user_category"  # Delta table the model reads
SECURITY_USER_COL    = "UserEmail"
MODEL_SECURITY_TABLE = "SecurityUserCategory"           # its table name inside the semantic model
RLS_ROLE             = "Sales Access"                   # the one dynamic role
SYNC_ROLE_MEMBERS    = True          # False when one Entra group is the role's member
REFRESH_MODEL        = True          # refresh (Import) / reframe (Direct Lake) the security table

# Safety stops
MAX_REMOVE_PCT          = 0.10       # stop if > 10% of the role's members would be removed
MAX_SECURITY_SHRINK_PCT = 0.10       # stop if > 10% of security rows would be removed

# ---------------- Source column mapping (edit to match your table) ----------------
# Set optional columns to None if your table doesn't have them.
COL = {
    "user":        "User",            # alias or full UPN / email          (required)
    "value":       "Category",        # entitled product category          (required)
    "start":       "StartDate",       # entitlement start date             (required)
    "end":         "EndDate",         # entitlement end date, null = open  (required)
    "app":         "ApplicationCode", # application filter                 (optional)
    "access":      "AccessFlag",      # access flag                        (optional)
    "delegation":  "OnBehalfOf",      # delegation marker                  (optional)
    "txn_id":      "TransactionId",   # source transaction id for audit    (optional)
    "modified_on": "ModifiedOn",      # last modified timestamp            (optional)
}

# ---------------- Normalise parameters (pipelines pass strings) ----------------
def to_bool(v):
    return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "y")

DRY_RUN            = to_bool(DRY_RUN)
INCLUDE_DELEGATION = to_bool(INCLUDE_DELEGATION)
SYNC_ROLE_MEMBERS  = to_bool(SYNC_ROLE_MEMBERS)
REFRESH_MODEL      = to_bool(REFRESH_MODEL)
MAX_REMOVE_PCT     = float(MAX_REMOVE_PCT)
MAX_SECURITY_SHRINK_PCT = float(MAX_SECURITY_SHRINK_PCT)
EQUIVALENT_DOMAINS = [d.strip().lower() for d in EQUIVALENT_DOMAINS]
print(f"DRY_RUN = {DRY_RUN} | SYNC_ROLE_MEMBERS = {SYNC_ROLE_MEMBERS}")

run_id = str(uuid.uuid4())
run_ts = datetime.now(timezone.utc)
today  = F.current_date()
audit_rows = []
failures = {}

def audit(action, role=None, member=None, txn=None, end=None, status=None, error=None, scope_key=None):
    audit_rows.append((run_id, run_ts, action, role, member,
                       None if scope_key is None else str(scope_key), txn, end,
                       status or ("DRY_RUN" if DRY_RUN else "APPLIED"), error))

# ---------------- Helpers ----------------
def as_date(df, col):
    """Return a date column whatever the source type (date, timestamp or string)."""
    dtype = df.schema[col].dataType
    if isinstance(dtype, DateType):
        return F.col(col)
    if isinstance(dtype, TimestampType):
        return F.to_date(F.col(col))
    c = F.trim(F.col(col).cast(StringType()))
    return F.coalesce(
        F.to_date(c, "yyyy-MM-dd"),
        F.to_date(c, "yyyy-MM-dd HH:mm:ss"),
        F.to_date(c, "dd-MM-yyyy"),
        F.to_date(c, "MM/dd/yyyy"),
        F.to_date(c, "dd/MM/yyyy"),
    )

def norm_member(name):
    """Lowercase; turn alias / alias@<equivalent domain> into alias@UPN_DOMAIN."""
    if not name:
        return None
    n = name.strip().lower()
    if "@" not in n:
        return f"{n}@{UPN_DOMAIN}"
    alias, domain = n.split("@", 1)
    return f"{alias}@{UPN_DOMAIN}" if domain in EQUIVALENT_DOMAINS else n

def key(name):
    """Compare names ignoring leading/trailing/double spaces and case."""
    return " ".join(str(name).split()).lower() if name is not None else None

def opt_col(k):
    name = COL.get(k)
    return F.col(name) if name else F.lit(None).cast(StringType())

def norm_text(c):
    return F.lower(F.regexp_replace(F.trim(c), r"\s+", " "))

# =====================================================================
# Read the role and its current members (read-only)
# =====================================================================
with connect_semantic_model(dataset=MODEL, workspace=WORKSPACE, readonly=True) as tom:
    roles = {key(r.Name): r for r in tom.model.Roles}
    print("Model roles (exact, quoted):", [repr(r.Name) for r in tom.model.Roles])
    role_obj = roles.get(key(RLS_ROLE))
    ROLE = role_obj.Name if role_obj is not None else None
    stored_name = {norm_member(m.MemberName): m.MemberName for m in role_obj.Members} if role_obj is not None else {}

if SYNC_ROLE_MEMBERS and ROLE is None:
    raise Exception(f"Role {RLS_ROLE!r} not found in the model. Create it first (see README).")

# =====================================================================
# Step 1: read entitlements, work out active / expired
# =====================================================================
raw = spark.read.table(SOURCE_TABLE)
raw = raw.toDF(*[c.strip() for c in raw.columns])          # trim stray spaces in column names
user   = F.lower(F.trim(F.col(COL["user"])))
alias  = F.split(user, "@").getItem(0)
domain = F.split(user, "@").getItem(1)

src = (raw
    .filter(user.isNotNull() & (user != ""))
    # full UPN: alias -> alias@UPN_DOMAIN; equivalent domains -> UPN_DOMAIN (same as norm_member)
    .withColumn("upn", F.when(~user.contains("@") | domain.isin(EQUIVALENT_DOMAINS),
                              F.concat(alias, F.lit("@" + UPN_DOMAIN)))
                        .otherwise(user))
    .withColumn("value", F.trim(F.col(COL["value"])))
    .filter(F.col("value").isNotNull() & (F.col("value") != ""))
    .withColumn("start_dt", as_date(raw, COL["start"]))
    .withColumn("end_dt",   as_date(raw, COL["end"]))
    .withColumn("txn_id",   opt_col("txn_id").cast(StringType()))
    .withColumn("end_raw",  F.col(COL["end"]).cast(StringType()))
    .withColumn("mod_on",   opt_col("modified_on"))
)
if APPLICATION_CODE and COL.get("app"):
    src = src.filter(F.trim(F.col(COL["app"])) == str(APPLICATION_CODE).strip())
if ACCESS_ALLOWED and COL.get("access"):
    src = src.filter(F.trim(F.col(COL["access"])).isin(ACCESS_ALLOWED))
if not INCLUDE_DELEGATION and COL.get("delegation"):
    src = src.filter(F.col(COL["delegation"]).isNull() |
                     (F.trim(F.col(COL["delegation"])) == ""))

is_active  = (F.col("start_dt").isNull() | (F.col("start_dt") <= today)) & \
             (F.col("end_dt").isNull()   | (F.col("end_dt")   >= today))
is_expired = F.col("end_dt").isNotNull() & (F.col("end_dt") < today)

w = Window.partitionBy("upn", "value").orderBy(F.col("mod_on").desc_nulls_last(),
                                                F.col("txn_id").desc_nulls_last())
src = src.withColumn("rn", F.row_number().over(w))
active_df = src.filter(is_active)

cols = ["upn", "value", "txn_id", "end_raw", "rn"]
active_rows  = active_df.select(*cols).collect()
expired_rows = src.filter(is_expired).select(*cols).collect()

# =====================================================================
# Step 2: rewrite the security table (UserEmail, CategoryKey)
# =====================================================================
scope = (spark.read.table(SCOPE_TABLE)
         .select(F.col(SCOPE_NAME_COL).alias("scope_name"), F.col(SCOPE_KEY_COL).alias("scope_key"))
         .dropDuplicates(["scope_key"])
         .withColumn("scope_match", norm_text(F.col("scope_name"))))
act = active_df.withColumn("value_match", norm_text(F.col("value")))
all_match = key(ALL_VALUE)
sec_new = (act.join(scope, (act.value_match == scope.scope_match) |
                           (act.value_match == F.lit(all_match)), "inner")
              .select(F.col("upn").alias(SECURITY_USER_COL), F.col("scope_key").alias(SCOPE_KEY_COL))
              .distinct())

known = {key(r.scope_name) for r in scope.select("scope_name").collect()} | {all_match}
unmapped = sorted({r.value for r in active_rows if key(r.value) not in known})

new_pairs = {(norm_member(r[SECURITY_USER_COL]), str(r[SCOPE_KEY_COL])) for r in sec_new.collect()}
try:
    old_pairs = {(norm_member(r[SECURITY_USER_COL]), str(r[SCOPE_KEY_COL]))
                 for r in spark.read.table(SECURITY_TABLE).collect()}
except Exception:
    old_pairs = set()       # first run: the table doesn't exist yet
sec_add, sec_remove = new_pairs - old_pairs, old_pairs - new_pairs
print(f"Security table: {len(old_pairs)} -> {len(new_pairs)} rows (+{len(sec_add)} / -{len(sec_remove)})")

if old_pairs and not new_pairs:
    raise Exception("Safety stop: the security table would become empty. Check the source load.")
if old_pairs and len(sec_remove) / len(old_pairs) > MAX_SECURITY_SHRINK_PCT:
    raise Exception(f"Safety stop: {len(sec_remove)} of {len(old_pairs)} security rows would be removed "
                    f"(> {MAX_SECURITY_SHRINK_PCT:.0%}). Check the source load.")

for u, k in sorted(sec_add):
    audit("SEC_ADD", MODEL_SECURITY_TABLE, u, scope_key=k)
for u, k in sorted(sec_remove):
    audit("SEC_REMOVE", MODEL_SECURITY_TABLE, u, scope_key=k)

if not DRY_RUN and (sec_add or sec_remove or not old_pairs):
    (sec_new.write.mode("overwrite").option("overwriteSchema", "true")
            .format("delta").saveAsTable(SECURITY_TABLE))
    print(f"Wrote {len(new_pairs)} rows to {SECURITY_TABLE}")

# =====================================================================
# Step 3: role members (anyone with a mapped active entitlement is in)
# =====================================================================
to_add, to_remove, trace = set(), set(), {}
if SYNC_ROLE_MEMBERS:
    desired = {norm_member(r.upn) for r in active_rows  if key(r.value) in known}
    expired = {norm_member(r.upn) for r in expired_rows} - desired   # only expired rows left
    current = set(stored_name)
    to_add    = desired - current
    to_remove = expired & current
    for r in sorted(expired_rows + active_rows, key=lambda x: x.rn, reverse=True):
        trace[norm_member(r.upn)] = (r.txn_id, r.end_raw)              # latest row wins
    print(f"Role {ROLE!r}: {len(current)} members | add {len(to_add)} | remove {len(to_remove)}")

    if current and len(to_remove) / len(current) > MAX_REMOVE_PCT:
        raise Exception(f"Safety stop: {len(to_remove)} of {len(current)} members would be removed "
                        f"from {ROLE!r} (> {MAX_REMOVE_PCT:.0%}). Check the source load.")

# =====================================================================
# Step 4: save member changes over XMLA, then refresh the security table
# =====================================================================
member_status = ("DRY_RUN", None) if DRY_RUN else ("SKIPPED", None)
if (to_add or to_remove) and not DRY_RUN:
    try:
        with connect_semantic_model(dataset=MODEL, workspace=WORKSPACE, readonly=False) as tom:
            if to_add:
                tom.add_role_member(role_name=ROLE, member=sorted(to_add))
            if to_remove:
                tom.remove_role_member(role_name=ROLE, member=[stored_name[m] for m in sorted(to_remove)])
        member_status = ("APPLIED", None)          # saved when the connection closes
    except Exception as e:
        member_status = ("FAILED", str(e)[:2000])
        failures["members"] = member_status[1]

for m in sorted(to_add):
    audit("ADD", ROLE, m, *trace.get(m, (None, None)), status=member_status[0], error=member_status[1])
for m in sorted(to_remove):
    audit("REMOVE", ROLE, stored_name[m], *trace.get(m, (None, None)),
          status=member_status[0], error=member_status[1])

if REFRESH_MODEL:
    if DRY_RUN:
        audit("REFRESH", MODEL_SECURITY_TABLE)
    else:
        try:
            from sempy_labs import refresh_semantic_model
            refresh_semantic_model(dataset=MODEL, workspace=WORKSPACE, tables=[MODEL_SECURITY_TABLE])
            audit("REFRESH", MODEL_SECURITY_TABLE)
        except Exception as e:
            audit("REFRESH", MODEL_SECURITY_TABLE, status="FAILED", error=str(e)[:2000])
            failures["refresh"] = str(e)[:2000]

for v in unmapped:
    audit("UNMAPPED_VALUE", None, v, status="DRY_RUN" if DRY_RUN else "LOGGED")

# =====================================================================
# Audit log
# =====================================================================
if audit_rows:
    audit_df = spark.createDataFrame(
        audit_rows,
        "run_id string, run_ts timestamp, action string, role_name string, member string, "
        "scope_key string, source_txn_id string, source_end_date string, status string, error string")
    (audit_df.write.mode("append").option("mergeSchema", "true")
             .format("delta").saveAsTable(AUDIT_TABLE))
    display(audit_df)

print(f"Run {run_id}: {'DRY_RUN' if DRY_RUN else 'APPLIED'} | security +{len(sec_add)}/-{len(sec_remove)} | "
      f"members +{len(to_add)}/-{len(to_remove)} | unmapped {unmapped} | failed {list(failures)}")
if failures:
    raise Exception(f"Sync failed: {list(failures)}. See {AUDIT_TABLE} for run {run_id}.")
