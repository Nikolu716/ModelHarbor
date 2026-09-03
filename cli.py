"""CLI 管理工具。

通过网关的 /admin API 操作，用法:
    python cli.py status                 # 查看所有中转站状态
    python cli.py probe <站点>           # 立即探测某站
    python cli.py enable <站点>          # 启用某站
    python cli.py disable <站点>         # 禁用某站
    python cli.py log [--tail 30]        # 查看最近事件
    python cli.py ping                   # 检查网关连通性

常用选项:
    --host http://127.0.0.1:8000  网关地址
"""
from __future__ import annotations

import argparse
import json
import time

import httpx
from rich.console import Console
from rich.table import Table

console = Console()

STATUS_LABEL = {"up": "在线", "down": "熔断", "cooling": "冷却中"}
STATUS_COLOR = {"up": "green", "down": "red", "cooling": "yellow"}


def _client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base.rstrip("/"), timeout=10.0)


def cmd_status(c: httpx.Client):
    data = c.get("/admin/status").json()
    table = Table(title="中转站状态（动态托管 · 按健康评分排序）")
    table.add_column("排名", justify="right")
    table.add_column("站点", style="bold")
    table.add_column("状态", no_wrap=True)
    table.add_column("评分", justify="right")
    table.add_column("计费")
    table.add_column("成功率\n(近100)", justify="right")
    table.add_column("延迟(EMA)", justify="right")
    table.add_column("额度", justify="right")
    table.add_column("调用", justify="right")
    table.add_column("切换出", justify="right")
    table.add_column("最近错误", overflow="fold", max_width=40)

    for st in sorted(data["stations"], key=lambda s: s["rank"]):
        label = STATUS_LABEL.get(st["status"], st["status"])
        color = STATUS_COLOR.get(st["status"], "white")
        if st.get("quota_exhausted"):
            label, color = "配额耗尽", "magenta"
        lat = "-" if st["ema_latency"] is None else f"{st['ema_latency'] * 1000:.0f}ms"
        score = st["health_score"]
        score_color = "green" if score >= 70 else ("yellow" if score >= 40 else "red")
        rate = f"{st['window_success_rate'] * 100:.0f}%"
        bal = st.get("balance")
        if bal is None:
            bal_txt = "-"
        elif bal < 1.0:
            bal_txt = f"[yellow]${bal:.2f}[/yellow]"
        else:
            bal_txt = f"[green]${bal:.2f}[/green]"
        err = (st["last_error"] or "-")
        err = err if len(err) <= 40 else err[:37] + "..."
        billing = "订阅" if st.get("billing") == "subscription" else "余额"
        table.add_row(
            f"#{st['rank']}", st["name"], f"[{color}]{label}[/{color}]",
            f"[{score_color}]{score:.0f}[/{score_color}]", billing,
            rate, lat, bal_txt, str(st["calls"]), str(st["switch_out"]), err,
        )

    s = data["summary"]
    console.print(table)
    console.print(f"汇总: [green]{s['up']}[/green]/{s['total']} 在线 | "
                  f"单请求最大尝试 {data['gateway']['max_attempts']} 次 | "
                  f"熔断冷却 {data['gateway']['cool_down']}s")


def cmd_probe(c: httpx.Client, name: str):
    r = c.post(f"/admin/station/{name}/probe")
    if r.status_code == 404:
        console.print(f"[red]中转站不存在: {name}[/red]")
        return 1
    if r.status_code == 400:
        console.print(f"[yellow]{r.json().get('detail', '探测失败')}[/yellow]")
        return 1
    d = r.json()
    if d["ok"]:
        console.print(f"[green]{name} 探测成功[/green] "
                      f"(延迟 {d['latency_ms']}ms)")
    else:
        console.print(f"[red]{name} 探测失败[/red]: {d['error']}")
    return 0


def cmd_toggle(c: httpx.Client, name: str, enable: bool):
    r = c.post(f"/admin/station/{name}/toggle")
    if r.status_code == 404:
        console.print(f"[red]中转站不存在: {name}[/red]")
        return 1
    state = "启用" if r.json()["enabled"] else "禁用"
    console.print(f"[green]{name} 已{state}[/green]")
    return 0


def cmd_reset(c: httpx.Client, name: str):
    r = c.post(f"/admin/station/{name}/reset")
    if r.status_code == 404:
        console.print(f"[red]中转站不存在: {name}[/red]")
        return 1
    d = r.json()
    if d["quota_exhausted"]:
        console.print(f"[yellow]{name} 仍无额度，将继续自动探测:[/yellow]")
        console.print(f"  {d.get('probe_error') or '未知'}")
        return 1
    console.print(f"[green]{name} 额度已恢复[/green]"
                  f" 状态={d['status']} 探测={'正常' if d['probe_ok'] else '异常'}")
    return 0


def cmd_log(c: httpx.Client, tail: int):
    d = c.get(f"/admin/logs", params={"tail": tail}).json()
    table = Table(title=f"最近 {len(d['events'])} 条事件")
    table.add_column("时间")
    table.add_column("站点")
    table.add_column("模型")
    table.add_column("结果")
    table.add_column("延迟", justify="right")
    table.add_column("详情", overflow="fold")
    for e in d["events"]:
        ok = "✓ 成功" if e["ok"] else "✗ 失败"
        color = "green" if e["ok"] else "red"
        lat = f"{e['latency_ms']:.0f}ms" if e["latency_ms"] else "-"
        table.add_row(
            time.strftime("%H:%M:%S", time.localtime(e["ts"])),
            e["station"], e["model"], f"[{color}]{ok}[/{color}]",
            lat, e.get("detail", ""),
        )
    console.print(table)


def cmd_ping(c: httpx.Client):
    try:
        d = c.get("/admin/ping").json()
        console.print(f"[green]网关在线[/green] ({d['ts']:.0f})")
        return 0
    except httpx.HTTPError:
        console.print("[red]网关不可达[/red]")
        return 1


def main():
    parser = argparse.ArgumentParser(description="中转站网关 CLI")
    parser.add_argument("--host", default="http://127.0.0.1:8000",
                        help="网关地址 (默认 http://127.0.0.1:8000)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看状态")
    p_probe = sub.add_parser("probe", help="立即探测")
    p_probe.add_argument("name")
    for name, act in (("enable", True), ("disable", False)):
        p = sub.add_parser(name, help=f"{'启用' if act else '禁用'}某站")
        p.add_argument("name")
    p_reset = sub.add_parser("reset", help="立即探测额度是否恢复（后台也会自动探测）")
    p_reset.add_argument("name")
    p_log = sub.add_parser("log", help="查看事件日志")
    p_log.add_argument("--tail", type=int, default=30)
    sub.add_parser("ping", help="检查连通性")

    args = parser.parse_args()
    c = _client(args.host)

    if args.cmd == "status":
        try:
            cmd_status(c)
        except httpx.HTTPError as e:
            console.print(f"[red]连接网关失败: {e}[/red]")
            return 1
    elif args.cmd == "probe":
        return cmd_probe(c, args.name)
    elif args.cmd == "enable":
        return cmd_toggle(c, args.name, True)
    elif args.cmd == "disable":
        return cmd_toggle(c, args.name, False)
    elif args.cmd == "reset":
        return cmd_reset(c, args.name)
    elif args.cmd == "log":
        cmd_log(c, args.tail)
    elif args.cmd == "ping":
        return cmd_ping(c)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
