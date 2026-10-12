from diagnostics import run_diagnostics
from settings import load_settings

if __name__ == "__main__":
    print(run_diagnostics(load_settings()))
