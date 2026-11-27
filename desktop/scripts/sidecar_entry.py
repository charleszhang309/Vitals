from importlib import import_module
import sys

if __name__ == "__main__":
    if sys.platform == "darwin" and sys.argv[1:2] == ["--mac-agent"]:
        del sys.argv[1]
        raise SystemExit(import_module("mac_agent.agent").main())

    from backend.main import main

    main()
