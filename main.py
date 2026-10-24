"""SigMarker 启动入口: python main.py。"""

import sys


def main():
    # 在加载 Qt 等依赖前进入自检，以便记录 DLL 错误并保持后台运行。
    if len(sys.argv) > 1 and sys.argv[1] == '--self-test':
        from sigmarker.package_smoke import run
        return run(sys.argv[2] if len(sys.argv) > 2 else None)

    from sigmarker.diagnostics import configure
    configure()
    from sigmarker.app import main as run_app
    return run_app()


if __name__ == '__main__':
    sys.exit(main())
