"""Run the registered v2.7 semiconductor closed-market grid matrix."""
from __future__ import annotations

import argparse, csv, json, statistics, sys
from dataclasses import dataclass
from datetime import datetime, time, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from core.models import GridDirectionMode
from core.scheduler import Scheduler
from data_sources.models import FundingEvent
from strategy.adaptive_grid import AdaptiveGridConfig
from strategy.backtest import BacktestConfig, run_grid_backtest
from strategy.grid_viability import GridViabilityConfig
from strategy.semiconductor_grid import (
    RESEARCH_SYMBOLS, StrategyAdmissionError, build_semiconductor_grid_candidate,
    long_signal_from_mapping, profiles_from_mapping, symbol_profiles_from_mapping,
)

UTC = timezone.utc
SEEDS = (3, 10, 17, 31, 59, 97)
DEFAULT_OUTPUT = Path("reports/semiconductor-grid-v2.7")

@dataclass(frozen=True)
class ClosedWindow:
    window_key: str
    market_group: str
    rows: tuple[dict[str, Any], ...]
    @property
    def start_time(self) -> datetime: return _row_dt(self.rows[0])
    @property
    def end_time(self) -> datetime: return _row_dt(self.rows[-1])

@dataclass(frozen=True)
class RuleSnapshot:
    tick_size: float = 0.0
    step_size: float = 0.0
    min_qty: float = 0.0
    min_notional: float = 0.0

@dataclass(frozen=True)
class Scenario:
    name: str
    maker_fee_rate: float
    taker_fee_rate: float
    maker_fill_probability: float
    max_fills_per_bar: int
    stop_slippage_bps: float

def build_closed_windows(rows: Iterable[dict[str, Any]], scheduler: Any, market_group: str) -> list[ClosedWindow]:
    groups: list[ClosedWindow] = []
    key = None; bucket: list[dict[str, Any]] = []
    for row in rows:
        window = scheduler.classify_window(_row_dt(row))
        current = window.window_key if window.allowed else None
        if current != key:
            if key and bucket: groups.append(ClosedWindow(key, market_group, tuple(bucket)))
            key, bucket = current, []
        if current: bucket.append(row)
    if key and bucket: groups.append(ClosedWindow(key, market_group, tuple(bucket)))
    return groups

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="回测 SNDK/MU/SOXL/SKHYNIX 休市窗口密集网格")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--data-dir", default="data/backtests/semiconductor-v2.7")
    p.add_argument("--rules-json")
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    p.add_argument("--symbols", nargs="*", default=list(RESEARCH_SYMBOLS))
    p.add_argument("--allow-missing-rules", action="store_true")
    p.add_argument("--allow-missing-funding", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    return p

def main() -> None:
    args = _parser().parse_args()
    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    cfg = raw.get("semiconductor_grid", {}) or {}
    output = Path(args.output_dir)
    if output.exists() and any(output.iterdir()) and not args.overwrite:
        raise RuntimeError(f"输出目录非空: {output}；传 --overwrite 才能覆盖")
    output.mkdir(parents=True, exist_ok=True)
    symbols = tuple(str(x).upper() for x in args.symbols)
    unknown = sorted(set(symbols) - set(RESEARCH_SYMBOLS))
    if unknown: raise ValueError("未注册标的: " + ", ".join(unknown))
    sprofiles = symbol_profiles_from_mapping(cfg.get("symbol_profiles", {}) or {})
    profiles = profiles_from_mapping(cfg.get("profiles", {}) or {})
    scenarios = _scenarios(cfg.get("execution", {}) or {})
    viability = _viability(cfg.get("viability", {}) or {})
    long_signal = long_signal_from_mapping(cfg.get("long_signal", {}) or {})
    base_grid = _grid(raw)
    observe = int(cfg.get("observation_rows", 180)); minimum = int(cfg.get("minimum_trade_rows", 120))
    capital = float(cfg.get("capital_per_symbol", 500)); leverage = float(cfg.get("economic_leverage", 1))
    data_dir = Path(args.data_dir)
    rules_path = Path(args.rules_json) if args.rules_json else data_dir / "exchange-rules.json"
    if not rules_path.exists() and not args.allow_missing_rules: raise FileNotFoundError(f"缺少规则快照: {rules_path}")
    rules = _load_rules(rules_path) if rules_path.exists() else {}
    rows_out: list[dict[str, Any]] = []; blocked: list[dict[str, Any]] = []; manifest = []
    for symbol in symbols:
        if symbol not in sprofiles: raise ValueError(f"缺少 {symbol} symbol_profile")
        csv_path = _find_csv(data_dir, symbol); bars = _read_klines(csv_path)
        funding_path = _funding_path(csv_path)
        if funding_path is None and not args.allow_missing_funding: raise FileNotFoundError(f"缺少 {symbol} Funding sidecar")
        funding = _read_funding(funding_path) if funding_path else []
        sp = sprofiles[symbol]
        scheduler = _scheduler(sp, cfg)
        windows = build_closed_windows(bars, scheduler, sp.market_group)
        manifest.append({"symbol":symbol,"csv":str(csv_path),"csv_sha256":_sha(csv_path),"funding":str(funding_path) if funding_path else None,"rows":len(bars),"closed_windows":len(windows),"calendar":sp.calendar_name})
        rule = rules.get(symbol, RuleSnapshot())
        if symbol not in rules and not args.allow_missing_rules: raise ValueError(f"规则快照缺少 {symbol}")
        for window in windows:
            if len(window.rows) < observe + minimum:
                blocked.append(_blocked(symbol, window, "ALL", "INSUFFICIENT_WINDOW_ROWS", str(len(window.rows)))); continue
            obs = list(window.rows[:observe]); trade = list(window.rows[observe:]); price = float(obs[-1]["close"])
            events = [x for x in funding if int(trade[0]["open_time"]) <= x.funding_time <= int(trade[-1]["close_time"])]
            prior = [x.funding_rate for x in funding if x.funding_time <= int(obs[-1]["close_time"])]
            funding_rate = prior[-1] if prior else 0.0; projected = sum(abs(x.funding_rate) for x in events)
            for profile in profiles:
                for scenario in scenarios:
                    try:
                        candidate = build_semiconductor_grid_candidate(symbol_profile=sp,strategy_profile=profile,klines=obs,current_price=price,funding_rate=funding_rate,projected_funding_pct=projected,maker_fee_rate=scenario.maker_fee_rate,regime_score=100.0,capital=capital,leverage=leverage,tick_size=rule.tick_size,step_size=rule.step_size,min_qty=rule.min_qty,min_notional=rule.min_notional,taker_fee_rate=scenario.taker_fee_rate,base_grid_config=base_grid,viability_config=viability,long_signal_config=long_signal)
                    except (StrategyAdmissionError, ValueError) as exc:
                        blocked.append(_blocked(symbol, window, profile.name, "ADMISSION_BLOCKED", str(exc), scenario.name)); continue
                    for seed in SEEDS:
                        result = run_grid_backtest(candidate.params, trade, current_price=price, config=BacktestConfig(capital=capital*sp.capital_multiplier,leverage=leverage,maker_fee_rate=scenario.maker_fee_rate,taker_fee_rate=scenario.taker_fee_rate,fill_model="L0_CONSERVATIVE",min_tick_size=rule.tick_size,quantity_step_size=rule.step_size,max_fills_per_bar=scenario.max_fills_per_bar,maker_fill_probability=scenario.maker_fill_probability,fill_probability_seed=seed,stop_slippage_bps=scenario.stop_slippage_bps,seed_slippage_bps=float(cfg.get("seed_slippage_bps",10)),force_close_at_end=True,direction_mode=profile.direction_mode,max_inventory_notional=capital*sp.capital_multiplier*float(cfg.get("max_inventory_multiplier",1)),inventory_caution_utilization=float(cfg.get("inventory_caution_utilization",.4)),inventory_critical_utilization=float(cfg.get("inventory_critical_utilization",.8)),max_unpaired_lots_per_side=int(cfg.get("max_unpaired_lots_per_side",8))), funding_events=events)
                        rows_out.append(_result_row(symbol, window, profile.name, scenario.name, seed, capital*sp.capital_multiplier, candidate, result))
    summary = _aggregate(rows_out, ("market_group","profile","scenario"))
    assessments = assess_profiles(summary, cfg.get("acceptance", {}) or {})
    conclusion = "SEMICONDUCTOR_GRID_RESEARCH_CANDIDATE" if any(x["passed"] for x in assessments) else ("NO_VALID_BACKTEST_RUNS" if not rows_out else "SEMICONDUCTOR_GRID_NOT_VALIDATED")
    payload = {"schema_version":1,"strategy_version":"semiconductor-grid-v2.7.0","generated_at":datetime.now(UTC).isoformat(),"conclusion":conclusion,"input_manifest":manifest,"profile_summary":summary,"assessments":assessments,"blocked_count":len(blocked),"run_count":len(rows_out)}
    _write_csv(output/"window-results.csv", rows_out); _write_csv(output/"blocked-windows.csv", blocked); _write_csv(output/"profile-summary.csv", summary); _write_csv(output/"assessment.csv", assessments)
    (output/"results.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    (output/"final-report.md").write_text(_report(payload),encoding="utf-8")
    print(json.dumps({"output":str(output),"conclusion":conclusion,"runs":len(rows_out),"blocked":len(blocked)},ensure_ascii=False))

def _result_row(symbol: str, window: ClosedWindow, profile: str, scenario: str, seed: int, capital: float, candidate: Any, result: Any) -> dict[str, Any]:
    paired = max(0.0,float(result.gross_grid_pnl)); drag = max(0.0,-float(result.unrealized_pnl)); inventory_drag = drag/max(paired,1e-12)
    return {"symbol":symbol,"market_group":window.market_group,"window_key":window.window_key,"window_start":window.start_time.isoformat(),"window_end":window.end_time.isoformat(),"profile":profile,"scenario":scenario,"seed":seed,"grid_num":candidate.params.grid_num,"step_pct":candidate.params.step_pct,"crossings_per_hour":candidate.viability.snapshot.crossings_per_hour,"net_capacity_per_hour":candidate.viability.snapshot.net_capacity_per_hour,"total_pnl":result.total_pnl,"gross_grid_pnl":result.gross_grid_pnl,"fees_paid":result.fees_paid,"funding_paid":result.funding_paid,"unrealized_pnl":result.unrealized_pnl,"inventory_drag_ratio":inventory_drag,"max_drawdown":result.max_drawdown,"max_drawdown_pct":result.max_drawdown/max(capital,1e-12),"pair_completion_count":result.pair_completion_count,"fills":len(result.fills),"stopped_reason":result.stopped_reason or ""}

def _aggregate(rows: list[dict[str, Any]], keys: tuple[str,...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any,...], list[dict[str,Any]]] = {}
    for row in rows: groups.setdefault(tuple(row[k] for k in keys),[]).append(row)
    output=[]
    for key, runs in sorted(groups.items()):
        by_symbol_window: dict[tuple[str,str], list[dict[str,Any]]] = {}
        for r in runs: by_symbol_window.setdefault((r["symbol"],r["window_key"]),[]).append(r)
        symbol_windows=[]
        for (symbol,window), seed_rows in by_symbol_window.items():
            symbol_windows.append({"symbol":symbol,"window_key":window,"pnl":statistics.fmean(float(x["total_pnl"]) for x in seed_rows),"drawdown":max(float(x["max_drawdown"]) for x in seed_rows),"drawdown_pct":max(float(x["max_drawdown_pct"]) for x in seed_rows),"fees":statistics.fmean(float(x["fees_paid"]) for x in seed_rows),"funding":statistics.fmean(float(x["funding_paid"]) for x in seed_rows),"drag":statistics.fmean(float(x["inventory_drag_ratio"]) for x in seed_rows)})
        by_window: dict[str,list[dict[str,Any]]] = {}
        for x in symbol_windows: by_window.setdefault(x["window_key"],[]).append(x)
        portfolios=[]
        for window, xs in by_window.items(): portfolios.append({"window_key":window,"pnl":sum(x["pnl"] for x in xs),"drawdown":sum(x["drawdown"] for x in xs),"drawdown_pct":max(x["drawdown_pct"] for x in xs),"fees":sum(x["fees"] for x in xs),"funding":sum(x["funding"] for x in xs),"drag":statistics.fmean(x["drag"] for x in xs)})
        pnls=[x["pnl"] for x in portfolios]; positives=[x for x in pnls if x>0]; losses=[x for x in pnls if x<0]
        total=sum(pnls); best=max(positives,default=0.0); concentration=best/sum(positives) if sum(positives)>0 else 1.0
        record={k:v for k,v in zip(keys,key)}
        record.update({"runs":len(runs),"unique_windows":len(portfolios),"total_pnl":total,"mean_window_pnl":statistics.fmean(pnls) if pnls else 0.0,"positive_ratio":len(positives)/len(pnls) if pnls else 0.0,"profit_factor":sum(positives)/abs(sum(losses)) if losses else (sum(positives) if positives else 0.0),"max_drawdown":max((x["drawdown"] for x in portfolios),default=0.0),"max_drawdown_pct":max((x["drawdown_pct"] for x in portfolios),default=0.0),"fees_paid":sum(x["fees"] for x in portfolios),"funding_paid":sum(x["funding"] for x in portfolios),"mean_inventory_drag_ratio":statistics.fmean(x["drag"] for x in portfolios) if portfolios else 0.0,"best_window_concentration":concentration})
        output.append(record)
    return output

def assess_profiles(summary: list[dict[str,Any]], acceptance: Mapping[str,Any]) -> list[dict[str,Any]]:
    limits={"minimum_unique_windows":int(acceptance.get("minimum_unique_windows",8)),"minimum_positive_ratio":float(acceptance.get("minimum_positive_ratio",.55)),"minimum_profit_factor":float(acceptance.get("minimum_profit_factor",1.05)),"maximum_drawdown_pct_of_capital":float(acceptance.get("maximum_drawdown_pct_of_capital",.05)),"maximum_mean_inventory_drag_ratio":float(acceptance.get("maximum_mean_inventory_drag_ratio",.35)),"maximum_best_window_concentration":float(acceptance.get("maximum_best_window_concentration",.35))}
    indexed={(x["market_group"],x["profile"],x["scenario"]):x for x in summary}; combos=sorted({(x["market_group"],x["profile"]) for x in summary}); out=[]
    for market,profile in combos:
        primary=indexed.get((market,profile,"PRIMARY_ZERO_MAKER")); stress=indexed.get((market,profile,"EXECUTION_STRESS")); reasons=[]
        if not primary: reasons.append("missing_primary")
        if not stress: reasons.append("missing_execution_stress")
        if primary:
            if int(primary["unique_windows"])<limits["minimum_unique_windows"]: reasons.append("insufficient_windows")
            if float(primary["total_pnl"])<=0: reasons.append("primary_not_positive")
            if float(primary["positive_ratio"])<limits["minimum_positive_ratio"]: reasons.append("low_positive_ratio")
            if float(primary["profit_factor"])<limits["minimum_profit_factor"]: reasons.append("low_profit_factor")
            if float(primary["max_drawdown_pct"])>limits["maximum_drawdown_pct_of_capital"]: reasons.append("drawdown_too_high")
            if float(primary["mean_inventory_drag_ratio"])>limits["maximum_mean_inventory_drag_ratio"]: reasons.append("inventory_drag_too_high")
            if float(primary["best_window_concentration"])>limits["maximum_best_window_concentration"]: reasons.append("window_concentration_too_high")
        if stress and float(stress["total_pnl"])<=0: reasons.append("execution_stress_not_positive")
        passed=not reasons; out.append({"market_group":market,"profile":profile,"passed":passed,"conclusion":"SEMICONDUCTOR_GRID_RESEARCH_CANDIDATE" if passed else "SEMICONDUCTOR_GRID_NOT_VALIDATED","reasons":";".join(reasons)})
    return out

def _scenarios(raw: Mapping[str,Any]) -> tuple[Scenario,...]:
    values=raw.get("scenarios",{}) if isinstance(raw,Mapping) else {}; result=[]
    for name,spec0 in values.items():
        spec=dict(spec0 or {}); result.append(Scenario(str(name).upper(),float(spec.get("maker_fee_rate",0)),float(spec.get("taker_fee_rate",.0005)),float(spec.get("maker_fill_probability",.65)),int(spec.get("max_fills_per_bar",2)),float(spec.get("stop_slippage_bps",10))))
    return tuple(result) or (Scenario("PRIMARY_ZERO_MAKER",0,.0005,.65,2,10),Scenario("EXECUTION_STRESS",0,.00075,.45,1,25),Scenario("MAKER_PROMO_OFF",.0002,.0005,.65,2,15))

def _grid(raw: Mapping[str,Any]) -> AdaptiveGridConfig:
    g=raw.get("grid",{}) or {}; c=raw.get("costs",{}) or {}; t=raw.get("trading",{}) or {}
    return AdaptiveGridConfig(center_half_life_minutes=float(g.get("center_half_life_minutes",30)),k_atr_range=float(g.get("k_atr_range",2)),k_sigma_range=float(g.get("k_sigma_range",2)),max_range_pct=float(g.get("max_range_pct",.03)),min_step_pct=float(g.get("min_step_pct",.0015)),max_step_pct=float(g.get("max_step_pct",.01)),k_atr_step=float(g.get("k_atr_step",.5)),k_sigma_step=float(g.get("k_sigma_step",.8)),min_grid_num=int(g.get("min_grid_num",3)),max_grid_num=int(g.get("max_grid_num",100)),expansion_rate=float(g.get("expansion_rate",.08)),stop_buffer_pct=float(t.get("stop_buffer_pct",.015)),adverse_selection_buffer_pct=float(c.get("adverse_selection_buffer_pct",.0002)),slippage_buffer_pct=float(c.get("slippage_buffer_pct",.0003)),safety_margin_pct=float(c.get("safety_margin_pct",.0002)),horizon_bars=int(g.get("horizon_bars",60)),volatility_estimator=str(g.get("volatility_estimator","ewma")))

def _viability(raw: Mapping[str,Any]) -> GridViabilityConfig:
    return GridViabilityConfig(lookback_bars=int(raw.get("lookback_bars",60)),bars_per_hour=float(raw.get("bars_per_hour",60)),min_crossings_per_hour=float(raw.get("min_crossings_per_hour",1)),min_reversal_ratio=float(raw.get("min_reversal_ratio",.25)),max_zero_activity_ratio=float(raw.get("max_zero_activity_ratio",.2)),min_trade_count_per_hour=float(raw.get("min_trade_count_per_hour",60)),min_quote_volume_per_hour=float(raw.get("min_quote_volume_per_hour",10000)),max_spread_to_step_ratio=float(raw.get("max_spread_to_step_ratio",.5)),min_net_capacity_per_hour=float(raw.get("min_net_capacity_per_hour",.00025)))

def _scheduler(sp: Any, cfg: Mapping[str,Any]) -> Scheduler:
    ref=None if sp.reference_open_time is None else time(*[int(x) for x in sp.reference_open_time.split(":")])
    return Scheduler(force_close_minutes=int(cfg.get("force_close_minutes",120)),minimum_trade_minutes=int(cfg.get("minimum_trade_minutes",120)),calendar_name=sp.calendar_name,market_timezone=sp.market_timezone,premarket_time=ref,window_key_prefix=sp.market_group)

def _read_klines(path: Path) -> list[dict[str,Any]]:
    out=[]
    with path.open(encoding="utf-8-sig",newline="") as h:
        for r in csv.DictReader(h):
            o=int(float(r.get("open_time") or r.get("timestamp") or 0)); c=int(float(r.get("close_time") or o+59999)); out.append({"open_time":o,"close_time":c,"timestamp":o,"open":float(r["open"]),"high":float(r["high"]),"low":float(r["low"]),"close":float(r["close"]),"volume":float(r.get("volume") or 0),"quote_volume":float(r.get("quote_volume") or 0),"trade_count":int(float(r.get("trade_count") or 0))})
    out.sort(key=lambda x:x["open_time"])
    if len({x["open_time"] for x in out})!=len(out): raise ValueError(f"{path} 存在重复时间")
    for i,x in enumerate(out):
        if x["close_time"]<=x["open_time"] or x["high"]<max(x["open"],x["close"]) or x["low"]>min(x["open"],x["close"]) or x["low"]<=0: raise ValueError(f"{path} 第 {i+1} 行非法")
        if i and x["open_time"]-out[i-1]["open_time"]!=60000: raise ValueError(f"{path} 1m 数据不连续")
    return out

def _read_funding(path: Path) -> list[FundingEvent]:
    p=json.loads(path.read_text(encoding="utf-8")); records=p.get("events",p) if isinstance(p,dict) else p
    return sorted((FundingEvent(int(x["funding_time"]),float(x["funding_rate"]),float(x["mark_price"]) if x.get("mark_price") not in (None,"") else None) for x in records or []),key=lambda x:x.funding_time)

def _load_rules(path: Path) -> dict[str,RuleSnapshot]:
    p=json.loads(path.read_text(encoding="utf-8")); records=p.get("symbols",p)
    return {str(k).upper():RuleSnapshot(float(v.get("tick_size",0)),float(v.get("step_size",0)),float(v.get("min_qty",0)),float(v.get("min_notional",0))) for k,v in records.items()}

def _find_csv(d: Path,s: str) -> Path:
    for p in (d/f"{s}-1m.csv",d/f"{s}.csv"):
        if p.exists(): return p
    m=sorted(d.glob(f"*{s}*1m*.csv"))
    if len(m)==1:return m[0]
    raise FileNotFoundError(f"未找到 {s} 1m CSV")

def _funding_path(csv_path: Path) -> Path|None:
    p=csv_path.with_suffix(".funding.json"); return p if p.exists() else None

def _row_dt(r: Mapping[str,Any]) -> datetime: return datetime.fromtimestamp(int(r["open_time"])/1000,tz=UTC)
def _blocked(s: str,w: ClosedWindow,p: str,c: str,r: str,scenario: str="") -> dict[str,Any]: return {"symbol":s,"market_group":w.market_group,"window_key":w.window_key,"window_start":w.start_time.isoformat(),"window_end":w.end_time.isoformat(),"profile":p,"scenario":scenario,"code":c,"reason":r}
def _sha(p: Path)->str: return sha256(p.read_bytes()).hexdigest()

def _write_csv(path: Path, rows: list[dict[str,Any]]) -> None:
    fields=sorted({k for r in rows for k in r}); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8",newline="") as h:
        if not fields:return
        w=csv.DictWriter(h,fieldnames=fields);w.writeheader();w.writerows(rows)

def _report(payload: Mapping[str,Any]) -> str:
    lines=["# Semiconductor Grid v2.7 Backtest", "",f"- Conclusion: `{payload['conclusion']}`",f"- Runs: {payload['run_count']}",f"- Blocked windows: {payload['blocked_count']}","","Neutral and LONG profiles are assessed separately. Primary Maker fee is zero, while funding, Taker exits, slippage, fill uncertainty and inventory risk remain included."]
    for x in payload["assessments"]: lines.append(f"- {x['market_group']} / {x['profile']}: `{x['conclusion']}` ({x['reasons'] or 'passed'})")
    return "\n".join(lines)+"\n"

if __name__ == "__main__": main()
