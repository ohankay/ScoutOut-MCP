"""What the MCP tools do, on top of the ScoutOut core. Every public method returns plain dicts of metadata and
counts; ``server.py`` passes them through ``guard``. No method returns a cell value."""

from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from scoutout.connector import open_database
from scoutout.connector.mssql import MsSqlAdapter
from scoutout.core.config import Config
from scoutout.sources import Source, config_for, get_source, save_source
from scoutout.store import Store, now

from .guard import kcount
from .settings import McpSettings

USER = "ajan"
SENSITIVE = ("SENSITIVE", "SPARSE")
ZONES = ("SENSITIVE", "SPARSE", "UNDECIDED", "CLEAN", "EMPTY")
SEVERITY = {"SENSITIVE": 0, "SPARSE": 1, "UNDECIDED": 2, "CLEAN": 3, "EMPTY": 4}
CACHE_S = 600


def source_name(database: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", database)[:40]


@dataclass
class Job:
    id: str
    databases: list[str]
    tables: list[str] | None
    force: bool
    status: str = "running"
    runs: dict = field(default_factory=dict)  # database -> run id
    errors: dict = field(default_factory=dict)  # database -> message
    started_at: str = field(default_factory=now)
    finished_at: str | None = None


class Service:
    def __init__(self, cfg: Config, mcp: McpSettings, store: Store | None = None, llm_client=None):
        self.cfg, self.mcp = cfg, mcp
        self.store = store or Store(cfg.paths.store)
        self.llm_client = llm_client
        self._dbs: dict[str, tuple[Source, dict]] = {}
        self._dbs_at = 0.0
        self._catalogs: dict[str, tuple[float, object]] = {}
        self._lock = threading.RLock()
        self.jobs: dict[str, Job] = {}

    # --- databases ------------------------------------------------------------------------------------
    def _register(self, database: str) -> Source:
        c = self.cfg.connection
        src = Source(source_name(database), c.server, database, c.auth, c.user, c.password_env, c.password_file,
                     c.encrypt, c.trust_server_certificate)
        old = get_source(self.store, src.name)
        if old is None or (old.server, old.database, old.user, old.password_env) != (src.server, src.database, src.user,
                                                                                       src.password_env):
            save_source(self.store, src, USER, self.cfg.paths.root)
        return src

    def databases(self, refresh: bool = False) -> dict[str, tuple[Source, dict]]:
        """Databases on the server the agent may work with: lower-case name -> (source, sys.databases info)."""
        with self._lock:
            if refresh or not self._dbs or time.monotonic() - self._dbs_at > CACHE_S:
                db = MsSqlAdapter(self.cfg.connection)
                try:
                    found = db.list_databases()
                finally:
                    db.close()
                wanted = {d.lower() for d in self.mcp.databases}
                excluded = {d.lower() for d in self.mcp.exclude_databases}
                out = {}
                for d in found:
                    key = d["name"].lower()
                    if key in excluded or (wanted and key not in wanted and key != self.mcp.masking_database.lower()):
                        continue
                    out[key] = (self._register(d["name"]), d)
                self._dbs, self._dbs_at = out, time.monotonic()
            return self._dbs

    def resolve(self, database: str) -> tuple[Source, Config]:
        dbs = self.databases()
        hit = dbs.get((database or "").strip().lower())
        if hit is None:
            dbs = self.databases(refresh=True)
            hit = dbs.get((database or "").strip().lower())
        if hit is None:
            raise ValueError(f"Veritabanı bulunamadı: {database}. Mevcut: {', '.join(s.database for s, _ in dbs.values())}")
        src = hit[0]
        return src, config_for(self.cfg, src)

    def catalog(self, database: str, refresh: bool = False):
        src, scfg = self.resolve(database)
        with self._lock:
            hit = self._catalogs.get(src.name)
            if hit and not refresh and time.monotonic() - hit[0] < CACHE_S:
                return hit[1]
        db, _ = open_database(scfg)  # refuses a login with write permission
        try:
            cat = db.discover_catalog()
        finally:
            db.close()
        with self._lock:
            self._catalogs[src.name] = (time.monotonic(), cat)
        return cat

    def table(self, database: str, table: str):
        cat = self.catalog(database)
        want = (table or "").strip().strip("[]").lower()
        exact = [t for t in cat.tables if t.full_name.lower() == want]
        if not exact:
            exact = [t for t in cat.tables if t.name.lower() == want]
        if not exact:
            raise ValueError(f"Tablo bulunamadı: {database}.{table}")
        if len(exact) > 1:
            raise ValueError(f"'{table}' birden fazla şemada var; şema.tablo yazın: " + ", ".join(t.full_name for t in exact))
        return exact[0]

    # --- decisions helpers --------------------------------------------------------------------------------
    def _decisions(self, source: str | None = None, schema: str | None = None, table: str | None = None) -> list:
        where, params = ["1=1"], []
        if source:
            where.append("d.source = ?")
            params.append(source)
        if schema:
            where.append("lower(d.schema_name) = lower(?)")
            params.append(schema)
        if table:
            where.append("lower(d.table_name) = lower(?)")
            params.append(table)
        return self.store.latest_decisions(" AND ".join(where), *params)

    @staticmethod
    def _effective(d) -> tuple[str, str | None]:
        if d["review_status"] in ("APPROVED", "OVERRIDDEN"):
            return d["final_zone"], d["final_type"]
        return d["zone"], d["sensitive_type"]

    def _masking_index(self):
        from scoutout.masking import index_for

        return index_for(self.store)

    def _finding(self, d, db_name: str, idx) -> dict:
        zone, dtype = self._effective(d)
        rec = json.loads(d["record_json"])
        info = idx.lookup(d["source"] or "", d["schema_name"], d["table_name"], d["column_name"]) if idx else None
        return {
            "database": db_name, "table": f"{d['schema_name']}.{d['table_name']}", "column": d["column_name"],
            "sql_type": d["sql_type"], "zone": zone, "sensitive_type": dtype,
            "rate": round(rec.get("p_corrected", 0.0), 4),
            "interval": [round(rec.get("ci_low", 0.0), 4), round(rec.get("ci_high", 0.0), 4)],
            "sample_size": rec.get("n", 0), "conflict": bool(d["conflict"]),
            "conflict_reason": rec.get("conflict_reason", "") if d["conflict"] else "",
            "review": {"APPROVED": "onaylandı", "OVERRIDDEN": "düzeltildi", "REJECTED": "reddedildi"}.get(d["review_status"] or "",
                                                                                                         "bekliyor"),
            "masking": {"algorithm": info["algorithm"], "term": info["term"]} if info else None,
            "last_scan": d["created_at"],
        }

    def _db_names(self) -> dict[str, str]:
        return {s.name: s.database for s, _ in self.databases().values()}

    # --- tools: discovery --------------------------------------------------------------------------------
    def list_databases(self) -> dict:
        rows = []
        for src, info in self.databases(refresh=True).values():
            last = self.store.one("SELECT MAX(finished_at) at FROM runs WHERE source=? AND status LIKE 'done%'", src.name)
            decs = self._decisions(src.name)
            zones = [self._effective(d)[0] for d in decs]
            rows.append({"database": src.database, "state": info["state"], "restored_at": info["created"],
                         "last_scan": last["at"] if last else None, "columns_scanned": len(decs),
                         "sensitive_columns": zones.count("SENSITIVE"), "sparse_columns": zones.count("SPARSE"),
                         "undecided_columns": zones.count("UNDECIDED")})
        from scoutout.masking import current

        row, _ = current(self.store)
        return {"databases": rows,
                "masking_list": {"version": row["version"], "columns": row["entry_count"], "at": row["fetched_at"]} if row else None}

    def list_tables(self, database: str, filter: str = "", only: str = "", limit: int = 200) -> dict:
        src, _ = self.resolve(database)
        cat = self.catalog(database)
        idx = self._masking_index()
        scanned = {r["table_name"]: r["at"] for r in self.store.q(
            "SELECT rt.table_name, MAX(COALESCE(rt.scanned_at, rt.finished_at)) at FROM run_tables rt JOIN runs r ON r.id = rt.run_id "
            "WHERE r.source = ? AND rt.status IN ('done','carried') GROUP BY rt.table_name", src.name)}
        by_table: dict[str, list] = {}
        for d in self._decisions(src.name):
            by_table.setdefault(f"{d['schema_name']}.{d['table_name']}", []).append(self._effective(d)[0])
        pat = (filter or "").strip().lower()
        rows = []
        for t in sorted(cat.tables, key=lambda t: t.full_name.lower()):
            name = t.full_name
            if pat and not (fnmatch.fnmatch(name.lower(), pat) if any(ch in pat for ch in "*?") else pat in name.lower()):
                continue
            zones = by_table.get(name, [])
            masked = sum(1 for c in t.columns if idx and idx.listed(src.name, t.schema, t.name, c.name))
            if only == "never_scanned" and name in scanned:
                continue
            if only == "sensitive" and not any(z in SENSITIVE for z in zones):
                continue
            if only == "masked" and not masked:
                continue
            rows.append({"table": name, "rows": t.row_count, "columns": len(t.columns), "last_scan": scanned.get(name),
                         "sensitive_columns": zones.count("SENSITIVE"), "sparse_columns": zones.count("SPARSE"),
                         "masked_columns": masked})
        total = len(rows)
        return {"database": src.database, "tables": rows[: max(1, min(limit, 1000))], "matched": total,
                "truncated": total > limit}

    def describe_table(self, database: str, table: str) -> dict:
        src, _ = self.resolve(database)
        t = self.table(database, table)
        idx = self._masking_index()
        decs = {d["column_name"]: d for d in self._decisions(src.name, t.schema, t.name)}
        cols = []
        for c in t.columns:
            info = idx.lookup(src.name, t.schema, t.name, c.name) if idx else None
            d = decs.get(c.name)
            f = self._finding(d, src.database, idx) if d else None
            cols.append({"column": c.name, "sql_type": c.type_label, "nullable": c.nullable,
                         "masking": {"algorithm": info["algorithm"], "term": info["term"]} if info else None,
                         "finding": {k: f[k] for k in ("zone", "sensitive_type", "rate", "interval", "conflict", "review",
                                                       "last_scan")} if f else None})
        return {"database": src.database, "table": t.full_name, "rows": t.row_count, "primary_key": t.primary_key,
                "columns": cols}

    # --- tools: scanning ---------------------------------------------------------------------------------
    def scan(self, databases: list[str] | None = None, tables: list[str] | None = None, force: bool = False) -> dict:
        with self._lock:
            busy = [j for j in self.jobs.values() if j.status == "running"]
            if busy:
                raise ValueError(f"Bir tarama zaten çalışıyor (job {busy[0].id}); scan_status ile izleyin.")
        names = databases or [s.database for s, _ in self.databases().values()
                              if s.database.lower() != self.mcp.masking_database.lower()]
        if not names:
            raise ValueError("Taranacak veritabanı yok.")
        plan = []
        for name in names:
            src, scfg = self.resolve(name)
            cat = self.catalog(name, refresh=True)
            if tables:
                chosen = [self.table(name, t) for t in tables if self._has_table(cat, t)]
                if not chosen:
                    raise ValueError(f"{src.database}: seçilen tabloların hiçbiri bu veritabanında yok.")
            else:
                chosen = cat.tables
            plan.append((src, scfg, [t.full_name for t in chosen] if tables else None, sum(len(t.columns) for t in chosen),
                         len(chosen)))
        job = Job(uuid.uuid4().hex[:8], [p[0].database for p in plan], tables, force)
        with self._lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run_job, args=(job, plan), daemon=True, name=f"job-{job.id}").start()
        columns = sum(p[3] for p in plan)
        return {"job_id": job.id, "databases": job.databases, "tables": sum(p[4] for p in plan), "columns": columns,
                "estimated_seconds": round(columns * 0.1 / max(self.cfg.scan.concurrency, 1)) + 5,
                "message": "Tarama başladı; hepsi yeniden taranıyor." if force else
                "Tarama başladı. Değişmemiş tablolar önceki sonuçla taşınır."}

    @staticmethod
    def _has_table(cat, name: str) -> bool:
        n = name.strip().strip("[]").lower()
        return any(t.full_name.lower() == n or t.name.lower() == n for t in cat.tables)

    def _run_job(self, job: Job, plan) -> None:
        from scoutout.knowledge import load_active_pack
        from scoutout.runner import Runner

        for src, scfg, only, _, _ in plan:
            try:
                pack = load_active_pack(self.cfg, self.store)
                runner = Runner(scfg, self.store, USER, pack=pack, source=src.name,
                                only_tables=[self.table(src.database, t).full_name for t in only] if only else None,
                                force=job.force)
                run_id = runner.start()
                job.runs[src.database] = run_id
                runner.execute(run_id)
            except Exception as e:  # noqa: BLE001 - one database must not stop the others
                from scoutout.core.log import scrub

                job.errors[src.database] = scrub(f"{type(e).__name__}: {e}")[:300]
        job.status = "failed" if job.errors and not job.runs else "done"
        job.finished_at = now()

    def scan_status(self, job_id: str | None = None) -> dict:
        from scoutout.runner import progress

        job = self.jobs.get(job_id) if job_id else (max(self.jobs.values(), key=lambda j: j.started_at) if self.jobs else None)
        if job is None:
            raise ValueError("Böyle bir tarama işi yok (sunucu yeniden başladıysa get_findings ile sonuçlara bakın).")
        dbs = []
        for name in job.databases:
            run_id = job.runs.get(name)
            if run_id:
                p = progress(self.store, run_id, self.cfg.scan.concurrency)
                r = self.store.one("SELECT status FROM runs WHERE id=?", run_id)
                carried = self.store.one("SELECT COUNT(*) n FROM run_tables WHERE run_id=? AND status='carried'", run_id)["n"]
                dbs.append({"database": name, "status": r["status"], "tables": p["total"], "done": p["done"],
                            "carried_unchanged": carried, "failed": p["failed"], "eta_seconds": p["eta_s"], "run_id": run_id})
            else:
                dbs.append({"database": name, "status": "failed" if name in job.errors else "waiting",
                            "error": job.errors.get(name, "")})
        return {"job_id": job.id, "status": job.status, "started_at": job.started_at, "finished_at": job.finished_at,
                "databases": dbs}

    def get_findings(self, database: str | None = None, table: str | None = None, zone: str | None = None,
                     sensitive_type: str | None = None, limit: int = 200) -> dict:
        src = self.resolve(database)[0] if database else None
        schema = tname = None
        if table:
            if src is None:
                raise ValueError("Tablo filtresi için veritabanı da verin.")
            t = self.table(database, table)
            schema, tname = t.schema, t.name
        names = self._db_names()
        idx = self._masking_index()
        zone = (zone or "").upper() or None
        if zone and zone not in ZONES + ("CONFLICT",):
            raise ValueError(f"zone şunlardan biri olmalı: {', '.join(ZONES + ('CONFLICT',))}")
        out = []
        for d in self._decisions(src.name if src else None, schema, tname):
            f = self._finding(d, names.get(d["source"], d["source"]), idx)
            if zone == "CONFLICT" and not f["conflict"]:
                continue
            if zone and zone != "CONFLICT" and f["zone"] != zone:
                continue
            if sensitive_type and (f["sensitive_type"] or "").upper() != sensitive_type.upper():
                continue
            out.append(f)
        summary: dict[str, int] = {}
        for f in out:
            summary[f["zone"]] = summary.get(f["zone"], 0) + 1
        out.sort(key=lambda f: (SEVERITY.get(f["zone"], 9), not f["conflict"], f["database"], f["table"], f["column"]))
        return {"summary": summary, "conflicts": sum(f["conflict"] for f in out), "findings": out[: max(1, min(limit, 2000))],
                "matched": len(out), "truncated": len(out) > limit}

    def explain_finding(self, database: str, table: str, column: str) -> dict:
        from scoutout.catalog import rule_label

        src, _ = self.resolve(database)
        t = self.table(database, table)
        d = next((x for x in self._decisions(src.name, t.schema, t.name) if x["column_name"].lower() == column.lower()), None)
        if d is None:
            raise ValueError(f"{t.full_name}.{column} için henüz sonuç yok; önce scan ile tarayın.")
        rec = json.loads(d["record_json"])
        k = self.mcp.k
        evidence = []
        for e in rec.get("evidence", []):
            evidence.append({
                "type": e["data_type"], "zone": e["zone"], "matched_rows": e["hits"], "sample_size": e["n"],
                "observed_rate": e["p_obs"], "corrected_rate": e["p_corrected"],
                "interval": [e["ci_low"], e["ci_high"]], "chance_matches_removed": e.get("chance_hits", 0),
                "by_rule": [{"rule": rule_label(det), "detector": det, "matched_rows": n}
                            for det, n in sorted((e.get("by_detector") or {}).items(), key=lambda kv: -kv[1])],
            })
        prof = rec.get("profile_summary") or {}
        n = prof.get("n", 0) or 0
        shapes = [{"shape": s, "share": share} for s, share in prof.get("top_shapes", []) if share * n >= k]
        sampling = rec.get("sampling", {})
        zone, dtype = self._effective(d)
        return {
            "database": src.database, "table": t.full_name, "column": d["column_name"], "zone": zone,
            "sensitive_type": dtype, "system_zone": d["zone"], "review": d["review_status"] or "bekliyor",
            "decision": _decision_text(rec), "conflict": bool(d["conflict"]), "conflict_reason": rec.get("conflict_reason", ""),
            "free_text": bool(rec.get("free_text")),
            "name_signal": {t_: round(s, 2) for t_, s in (rec.get("metadata_signal") or {}).items()},
            "evidence": evidence,
            "profile": {"null_rate": prof.get("null_rate"), "distinct_rate": prof.get("distinct_rate"),
                        "length": [prof.get("len_min"), prof.get("len_p50"), prof.get("len_max")],
                        "digit_rate": prof.get("digit_rate"), "alpha_rate": prof.get("alpha_rate"), "shapes": shapes},
            "sampling": {"strategy": sampling.get("strategy"), "rows": sampling.get("n_actual"),
                         "bias_flags": sampling.get("bias_flags", [])},
            "knowledge_pack": rec.get("knowledge_pack_version"),
        }

    # --- tools: masking ---------------------------------------------------------------------------------------
    def masking_sync(self) -> dict:
        from scoutout import masking

        src, _ = self.resolve(self.mcp.masking_database)
        want = {"source": src.name, "query": self.mcp.masking_query, "interval_min": 60}
        if self.store.get_setting(masking.SETTING) != want:
            self.store.set_setting(masking.SETTING, want, USER)
        res = masking.sync(self.store, self.cfg, USER)
        if not res.get("ok"):
            raise ValueError("Maskeleme listesi okunamadı: " + res.get("error", ""))
        return {"version": res["version"], "columns": res["count"], "changed": res["changed"],
                "added": len(res["added"]), "removed": len(res["removed"]), "algorithm_changed": len(res["algo_changed"]),
                "added_columns": res["added"][:30], "removed_columns": res["removed"][:30],
                "algorithm_changes": res["algo_changed"][:30]}

    def audit_masking(self, databases: list[str] | None = None, tables: list[str] | None = None,
                      scan_missing: bool = False) -> dict:
        from scoutout.masking import algo_fit, current

        row, entries = current(self.store)
        sync_note = None
        if row is None:
            self.masking_sync()
            row, entries = current(self.store)
        else:
            try:
                s = self.masking_sync()
                sync_note = f"liste güncel (v{s['version']})" + (f", {s['added']} eklenen, {s['removed']} çıkan" if s["changed"] else "")
            except ValueError as e:
                sync_note = f"liste güncellenemedi, saklı v{row['version']} kullanıldı: {e}"
            row, entries = current(self.store)
        idx = self._masking_index()
        srcs = [self.resolve(d)[0] for d in databases] if databases else \
            [s for s, _ in self.databases().values() if s.database.lower() != self.mcp.masking_database.lower()]
        want_tables = {t.strip().lower() for t in tables or []}

        def table_ok(schema: str, table: str) -> bool:
            return not want_tables or f"{schema}.{table}".lower() in want_tables or table.lower() in want_tables

        out = {"unnecessary": [], "missing": [], "algorithm": [], "uncertain": [], "correct": 0, "not_scanned": []}
        not_scanned: dict[str, set] = {}
        for src in srcs:
            decs = {(d["schema_name"].lower(), d["table_name"].lower(), d["column_name"].lower()): d
                    for d in self._decisions(src.name)}
            # listed columns of this database (entries naming it, or naming no database)
            for e in entries:
                if e[0] and e[0].lower() not in src.names:
                    continue
                if not table_ok(e[1], e[2]):
                    continue
                d = decs.get((e[1].lower(), e[2].lower(), e[3].lower()))
                if d is None:
                    if e[0] or self._has_table(self.catalog(src.database), f"{e[1]}.{e[2]}"):
                        not_scanned.setdefault(src.database, set()).add(f"{e[1]}.{e[2]}")
                    continue
                f = self._finding(d, src.database, idx)
                item = {k: f[k] for k in ("database", "table", "column", "zone", "sensitive_type", "rate", "interval",
                                          "conflict", "review")} | {"algorithm": e[4], "term": e[5]}
                if f["zone"] in SENSITIVE:
                    out["correct"] += 1
                    fit = algo_fit(e[4], f["zone"], f["sensitive_type"])
                    if fit:
                        out["algorithm"].append(item | {"issue": fit[0], "note": fit[1]})
                elif f["zone"] in ("CLEAN", "EMPTY") and not f["conflict"]:
                    out["unnecessary"].append(item | {"note": "Yüksek güvenle " + ("boş" if f["zone"] == "EMPTY" else "hassas veri yok")})
                else:
                    note = f["conflict_reason"] or "Sonuç belirsiz; daha fazla örneklem veya taban gerekir"
                    out["uncertain"].append(item | {"note": note})
            for d in decs.values():
                if not table_ok(d["schema_name"], d["table_name"]):
                    continue
                zone, _ = self._effective(d)
                if zone in SENSITIVE and not idx.listed(src.name, d["schema_name"], d["table_name"], d["column_name"]):
                    f = self._finding(d, src.database, idx)
                    out["missing"].append({k: f[k] for k in ("database", "table", "column", "zone", "sensitive_type", "rate",
                                                             "interval", "conflict", "review")})
        out["not_scanned"] = [{"database": db, "tables": sorted(ts)} for db, ts in not_scanned.items()]
        result = {"masking_list": {"version": row["version"], "columns": row["entry_count"], "note": sync_note},
                  "summary": {"unnecessary": len(out["unnecessary"]), "missing": len(out["missing"]),
                              "algorithm": len(out["algorithm"]), "uncertain": len(out["uncertain"]), "correct": out["correct"],
                              "tables_not_scanned": sum(len(x["tables"]) for x in out["not_scanned"])}} | out
        if scan_missing and not_scanned:
            todo = {db: sorted(ts) for db, ts in not_scanned.items()}
            jobs = []
            for db, ts in todo.items():  # one job at a time: the first database now, the agent repeats for the rest
                jobs.append(self.scan([db], ts))
                break
            result["scan_started"] = jobs[0]
            result["message"] = "Listede olup taranmamış tablolar taranıyor; bitince audit_masking'i tekrar çağırın."
        return result

    # --- tools: reviews ----------------------------------------------------------------------------------
    def review_finding(self, database: str, table: str, column: str, decision: str, reason: str,
                       final_zone: str | None = None, final_type: str | None = None) -> dict:
        src, _ = self.resolve(database)
        t = self.table(database, table)
        d = next((x for x in self._decisions(src.name, t.schema, t.name) if x["column_name"].lower() == column.lower()), None)
        if d is None:
            raise ValueError("Bu kolon için sonuç yok.")
        status = {"approve": "APPROVED", "override": "OVERRIDDEN", "reject": "REJECTED"}.get(decision)
        if status is None:
            raise ValueError("decision: approve, override veya reject olmalı.")
        zone = (final_zone or d["zone"]).upper()
        if status == "OVERRIDDEN" and zone not in ZONES:
            raise ValueError(f"final_zone şunlardan biri olmalı: {', '.join(ZONES)}")
        self.store.review(d["id"], status, zone, (final_type or d["sensitive_type"]) if zone in SENSITIVE else None, reason, USER)
        return {"database": src.database, "table": t.full_name, "column": d["column_name"], "review": status, "zone": zone}

    # --- tools: report ----------------------------------------------------------------------------------
    def export_report(self, kind: str = "findings", database: str | None = None) -> dict:
        from scoutout.report import TYPE_TR, ZONE_TR, _xlsx

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out_dir = self.cfg.paths.reports
        out_dir.mkdir(parents=True, exist_ok=True)

        def zt(z):
            return ZONE_TR.get(z, z or "")

        def tt(t):
            return TYPE_TR.get(t, t or "")

        if kind == "findings":
            rows = self.get_findings(database, limit=100000)["findings"]
            head = ["Veritabanı", "Tablo", "Kolon", "Veri tipi", "Sonuç", "Hassas veri tipi", "Oran", "Güven aralığı",
                    "Örneklem", "Çelişki", "Karar", "Maskeleme algoritması"]
            sheet = [head] + [[r["database"], r["table"], r["column"], r["sql_type"], zt(r["zone"]), tt(r["sensitive_type"]),
                               r["rate"], f"{r['interval'][0]}–{r['interval'][1]}", r["sample_size"],
                               r["conflict_reason"] if r["conflict"] else "", r["review"],
                               (r["masking"] or {}).get("algorithm") or ""] for r in rows]
            sheets = [("Bulgular", sheet, 3)]
            count = len(rows)
        elif kind == "masking_audit":
            a = self.audit_masking([database] if database else None)
            head = ["Veritabanı", "Tablo", "Kolon", "Sonuç", "Hassas veri tipi", "Oran", "Algoritma", "İş terimi", "Not"]

            def line(r):
                return [r["database"], r["table"], r["column"], zt(r["zone"]), tt(r["sensitive_type"]), r["rate"],
                        r.get("algorithm") or "", r.get("term") or "", r.get("note", "")]

            s = a["summary"]
            overview = [["Maskeleme denetimi", stamp], ["Maskeleme listesi", f"v{a['masking_list']['version']}"],
                        ["Gereksiz maskeleme", s["unnecessary"]], ["Eksik maskeleme", s["missing"]],
                        ["Algoritma gözden geçir", s["algorithm"]], ["Belirsiz", s["uncertain"]],
                        ["Doğru maskelenen", s["correct"]], ["Listede olup taranmamış tablo", s["tables_not_scanned"]]]
            sheets = [("Özet", overview, None)] + [(title, [head] + [line(r) for r in a[key]], 3) for key, title in
                                                   (("unnecessary", "Gereksiz maskeleme"), ("missing", "Eksik maskeleme"),
                                                    ("algorithm", "Algoritma"), ("uncertain", "Belirsiz"))]
            count = s["unnecessary"] + s["missing"] + s["algorithm"] + s["uncertain"]
        elif kind == "rules":
            rules = self.list_rules()["learned"]
            head = ["No", "Tür", "Kaynak", "Durum", "Desen", "Bağlam", "Doğrulama", "Kolon adları", "Duyarlılık", "Kesinlik", "Gerekçe"]
            sheet = [head] + [[r["id"], r["data_type"], r["source"], r["status"], r["pattern"], ", ".join(r["context"]),
                               r["checksum"] or "", ", ".join(r["column_names"]), r["metrics"].get("sensitivity"),
                               r["metrics"].get("precision"), r["reason"] or ""] for r in rules]
            sheets = [("Kurallar", sheet, 1)]
            count = len(rules)
        else:
            raise ValueError("kind: findings, masking_audit veya rules olmalı.")
        path = _xlsx(sheets, out_dir / f"scoutout_{kind}_{stamp}.xlsx")
        self.store.audit(USER, f"report.{kind}", str(path), rows=count)
        return {"path": str(path), "rows": count}

    # --- tools: rules ---------------------------------------------------------------------------------
    def rule_view(self, r) -> dict:
        body = json.loads(r["body_json"])
        m = json.loads(r["metrics_json"] or "{}")
        return {"id": r["id"], "data_type": r["data_type"], "source": r["source"], "status": r["status"],
                "pattern": body.get("pattern"), "context": body.get("context", []), "checksum": body.get("checksum"),
                "column_names": body.get("column_names", []), "ignore_case": bool(body.get("ignore_case")),
                "metrics": metrics_view(m, self.mcp.k), "reason": r["reason"], "decided_by": r["decided_by"],
                "at": r["decided_at"] or r["created_at"]}

    def list_rules(self, data_type: str | None = None, status: str | None = None) -> dict:
        from scoutout.catalog import GENERIC

        where, params = ["1=1"], []
        if data_type:
            where.append("data_type = ?")
            params.append(data_type.upper())
        if status:
            where.append("status = ?")
            params.append(status.upper())
        rows = self.store.q(f"SELECT * FROM rules WHERE {' AND '.join(where)} ORDER BY CASE status WHEN 'ACTIVE' THEN 0 "
                            "WHEN 'CANDIDATE' THEN 1 ELSE 2 END, id", *params)
        pack = self.store.one("SELECT version, summary_json FROM packs WHERE active=1")
        return {"generic": [{"detector": k, "name": v[0], "kind": v[1], "how": v[2]} for k, v in GENERIC.items()],
                "learned": [self.rule_view(r) for r in rows],
                "baseline": {"version": pack["version"], "summary": json.loads(pack["summary_json"]).get("text")} if pack else None}

    def decide_rule(self, rule_id: int, action: str, reason: str) -> dict:
        r = self.store.one("SELECT * FROM rules WHERE id=?", rule_id)
        if r is None:
            raise ValueError(f"Kural bulunamadı: {rule_id}")
        if not (reason or "").strip():
            raise ValueError("Gerekçe zorunlu.")
        status = {"activate": "ACTIVE", "reject": "REJECTED", "retire": "RETIRED"}.get(action)
        if status is None:
            raise ValueError("action: activate, reject veya retire olmalı.")
        if status == "ACTIVE" and r["status"] not in ("CANDIDATE", "RETIRED"):
            raise ValueError(f"Sadece aday veya emekli kural etkinleştirilebilir (şu an {r['status']}).")
        self.store.x("UPDATE rules SET status=?, decided_by=?, decided_at=?, reason=? WHERE id=?",
                     status, USER, now(), reason.strip(), rule_id)
        self.store.audit(USER, f"rule.{action}", str(rule_id), data_type=r["data_type"], reason=reason.strip())
        return {"id": rule_id, "status": status, "message": "Sonraki taramalar bu değişikliği kullanır. Etkisini görmek için "
                                                            "ilgili tabloları force ile yeniden tarayın."}

    # --- learning (see miner.py) ------------------------------------------------------------------------------
    def llm(self):
        """The local LLM client (allow-listed hosts only). Tests inject a fake one."""
        if self.llm_client is not None:
            return self.llm_client
        from scoutout.llm import LlmClient

        c = self.cfg.llm
        if not c.endpoint:
            raise ValueError("Yerel LLM ayarlanmamış: config.yaml → llm.endpoint (ör. http://localhost:11434/v1).")
        return LlmClient.from_config(c)

    def teach_baseline(self, entries: list[dict], negatives: list[dict] | None = None) -> dict:
        from . import miner

        return miner.teach_baseline(self, entries, negatives or [])

    def mine_rules(self, entries: list[dict], hint: str = "", max_rules: int = 8) -> dict:
        from . import miner

        return miner.mine_rules(self, entries, hint, max_rules)

    def propose_rule(self, rule: dict, positives: list[dict], negatives: list[dict] | None = None) -> dict:
        from . import miner

        return miner.propose_rule(self, rule, positives, negatives or [])

    def profile_column(self, database: str, table: str, column: str) -> dict:
        from . import miner

        return miner.profile_column(self, database, table, column)


def metrics_view(m: dict, k: int) -> dict:
    """Rule measurements as the agent sees them: rates, with small match counts hidden (k-anonymity)."""
    if not m:
        return {}
    tp, fp = m.get("tp"), m.get("fp")
    out = {"positives": m.get("positives"), "negatives": m.get("negatives")}
    if tp is not None and 0 < tp < k:
        out |= {"matched_positives": kcount(tp, k), "sensitivity": None, "precision": None}
    else:
        out |= {"matched_positives": tp, "sensitivity": m.get("sensitivity"), "precision": m.get("precision")}
    out |= {"matched_negatives": kcount(fp, k) if fp is not None else None, "specificity": m.get("specificity")}
    return out


def _decision_text(rec: dict) -> str:
    z = rec.get("zone")
    t = rec.get("sensitive_type") or ""
    if z == "SENSITIVE":
        return f"Değerlerin önemli kısmı {t}; alt güven sınırı hassas eşiğinin üstünde."
    if z == "SPARSE":
        return f"{t} değerlerin küçük bir kısmında var (seyrek); varlığı istatistiksel olarak kanıtlı."
    if z == "CLEAN":
        return "Hiçbir hassas tür için varlık kanıtı yok ve üst güven sınırı temiz eşiğinin altında."
    if z == "EMPTY":
        return "Kolon boş (dolu değer yok)."
    return "Örneklem kararı vermeye yetmedi; hassas ile temiz arasında."
