from __future__ import annotations

import argparse
import json
from pathlib import Path

from reelucid.component_jobs import COMPONENTS
from reelucid.component_manager.install import VERSIONS, Manager


def register(parser: argparse.ArgumentParser) -> None:
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument("--core-project", default="reelucid", help="主服务的 Compose 项目名")
    options.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".reelucid/components",
        help="宿主私有安装记录，默认位于当前用户目录",
    )
    group = parser.add_subparsers(dest="component_action", required=True)
    group.add_parser("list", parents=[options], help="查看本机安装器管理的组件")
    install = group.add_parser("install", parents=[options], help="部署或恢复指定组件")
    install.add_argument("component", choices=COMPONENTS)
    install.add_argument("--dtk-port", type=int, default=18080, help="新 DTK 安装的宿主环回端口")
    agent = group.add_parser("agent", parents=[options], help="为本机 Web 后台执行组件安装请求")
    agent.add_argument("--dtk-port", type=int, default=18080)
    agent.add_argument("--api-url", default="http://127.0.0.1:8000", help="主服务的本机回环 API")
    agent.add_argument("--token-file", type=Path, help="管理员 Token 文件，默认从主容器读取")


def execute(args: argparse.Namespace) -> None:
    manager = Manager(args.state_dir.resolve(), args.core_project, getattr(args, "dtk_port", 18080))
    if args.component_action == "agent":
        from reelucid.component_manager.agent import serve

        serve(manager, args.api_url, args.token_file)
        return
    if args.component_action == "install":
        value = manager.install(args.component)
    else:
        value = {
            "components": [
                {"id": k, "version": VERSIONS[k], "state": state}
                for k, state in manager.states().items()
            ]
        }
    print(json.dumps(value, ensure_ascii=False, indent=2))
