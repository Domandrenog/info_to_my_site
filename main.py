#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.ucoin_catalog import slugify

PROJECT_DIR = Path(__file__).resolve().parent


def line() -> None:
    print("-" * 72)


def title(text: str) -> None:
    line()
    print(text)
    line()


def ask_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    if not value:
        return default
    return value


def ask_int(prompt: str, default: int | None = None, allow_empty: bool = True) -> int | None:
    default_text = "" if default is None else str(default)
    while True:
        value = ask_text(prompt, default_text)
        if not value:
            if allow_empty:
                return None
            print("Valor obrigatorio.")
            continue
        try:
            return int(value)
        except ValueError:
            print("Escreve um numero inteiro valido.")


def ask_float(prompt: str, default: float) -> float:
    while True:
        value = ask_text(prompt, str(default))
        try:
            return float(value)
        except ValueError:
            print("Escreve um numero valido (ex: 3 ou 2.5).")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    default_token = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{default_token}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "s", "sim"}:
            return True
        if value in {"n", "no", "nao"}:
            return False
        print("Responde com y ou n.")


def run_step(step_name: str, command: list[str]) -> None:
    title(f"Executar: {step_name}")
    print("Comando:")
    print(" ".join(command))
    if not ask_yes_no("Queres executar agora?", default=True):
        print("Execucao cancelada pelo utilizador.")
        return

    try:
        subprocess.run(command, cwd=PROJECT_DIR, check=True)
        print("Concluido com sucesso.")
    except subprocess.CalledProcessError as exc:
        print(f"Falhou com codigo de saida {exc.returncode}.")


def explain_prerequisites() -> None:
    title("Guia rapido antes de comecar")
    print("1) Instala dependencias Python:")
    print("   python3 -m pip install -r requirements.txt")
    print("2) Se fores fazer scrape no uCoin com sessao manual:")
    print("   chromium --remote-debugging-port=9222")
    print("   Depois entra no uCoin nessa janela e resolve Cloudflare/login.")
    print("3) Para importar para Base44, cria .env com:")
    print("   BASE44_APP_ID=...")
    print("   BASE44_API_KEY=...")
    print("4) Fluxo normal recomendado:")
    print("   Menu 2 (pipeline completo) -> preencher app-catalog-final.json -> Enter no terminal")


def default_catalog_path(country: str) -> str:
    return str(Path("paises") / slugify(country) / "ucoin-catalog.json")


def default_app_catalog_path(country: str) -> str:
    return str(Path("paises") / slugify(country) / "app-catalog.json")


def default_final_input_path(country: str) -> str:
    return str(Path("paises") / slugify(country) / "app-catalog-final.json")


def ask_country_link_name() -> str:
    print("Se o link do uCoin usa um nome diferente do pais, podes indicar aqui.")
    if not ask_yes_no("O nome do link e diferente?", default=False):
        return ""
    return ask_text("Nome do link no uCoin (ex: belarus)", "")


def action_pipeline() -> None:
    title("Pipeline completo (scrape + pending/final)")
    print("Este passo executa o scrape do uCoin e chama generate_resume_json.py.")
    print("Se nao ativares 'no-wait-for-final', ele vai esperar o app-catalog-final.json.")

    country = ask_text("Pais (ex: India, Canada)")
    if not country:
        print("Pais obrigatorio.")
        return

    command = [sys.executable, "-m", "scripts.ucoin_pipeline", country]

    country_link_name = ask_country_link_name()
    if country_link_name:
        command.extend(["--country-link-name", country_link_name])

    start_year = ask_int("Start year (opcional)", default=None, allow_empty=True)
    if start_year is not None:
        command.extend(["--start-year", str(start_year)])

    attach_cdp = ask_yes_no("Usar sessao CDP aberta no browser?", default=True)
    if attach_cdp:
        cdp_url = ask_text("CDP URL", "http://127.0.0.1:9222")
        command.extend(["--attach-cdp", "--cdp-url", cdp_url])

    manual_session = ask_yes_no("Pausar para confirmacao manual (Cloudflare/login)?", default=True)
    if manual_session:
        command.append("--manual-session")
    else:
        command.append("--no-manual-session")

    timeout = ask_int("Timeout (segundos)", default=60, allow_empty=False)
    max_pages = ask_int("Max pages", default=50, allow_empty=False)
    retries = ask_int("Retries por pagina", default=2, allow_empty=False)
    command.extend(["--timeout", str(timeout), "--max-pages", str(max_pages), "--retries", str(retries)])

    if ask_yes_no("Parar apos gerar app-catalog-pending.json (sem esperar final)?", default=False):
        command.append("--no-wait-for-final")

    run_step("Pipeline completo", command)


def action_scrape_only() -> None:
    title("Scrape apenas (ucoin-catalog.json)")
    print("Este passo recolhe o catalogo tecnico do uCoin sem gerar ficheiros finais da app.")

    country = ask_text("Pais (ex: India, Canada)")
    if not country:
        print("Pais obrigatorio.")
        return

    command = [sys.executable, "-m", "scripts.ucoin_catalog", country, "--json"]

    country_link_name = ask_country_link_name()
    if country_link_name:
        command.extend(["--country-link-name", country_link_name])

    period = ask_text("Periodo (opcional, ex: 1957-2020)", "")
    if period:
        command.append(period)

    start_year = ask_int("Start year (opcional)", default=None, allow_empty=True)
    if start_year is not None:
        command.extend(["--start-year", str(start_year)])

    output_dir = ask_text("Output dir (opcional)", "")
    if output_dir:
        command.extend(["--output-dir", output_dir])

    attach_cdp = ask_yes_no("Usar sessao CDP aberta no browser?", default=True)
    if attach_cdp:
        cdp_url = ask_text("CDP URL", "http://127.0.0.1:9222")
        command.extend(["--attach-cdp", "--cdp-url", cdp_url])

    manual_session = ask_yes_no("Pausar para confirmacao manual (Cloudflare/login)?", default=True)
    if manual_session:
        command.append("--manual-session")
    else:
        command.append("--no-manual-session")

    timeout = ask_int("Timeout (segundos)", default=60, allow_empty=False)
    max_pages = ask_int("Max pages", default=50, allow_empty=False)
    retries = ask_int("Retries por pagina", default=2, allow_empty=False)
    command.extend(["--timeout", str(timeout), "--max-pages", str(max_pages), "--retries", str(retries)])

    run_step("Scrape uCoin", command)


def action_generate_pending() -> None:
    title("Gerar app-catalog-pending.json")
    print("Este passo transforma ucoin-catalog.json no formato simplificado para classificacao.")

    country = ask_text("Pais (para sugerir caminho default)", "India")
    default_input = default_catalog_path(country)
    input_path = ask_text("Input ucoin-catalog.json", default_input)

    command = [sys.executable, "-m", "scripts.generate_resume_json", "--input", input_path]
    if ask_yes_no("Esperar por app-catalog-final.json no fim?", default=True):
        command.append("--wait-for-final")

    run_step("Gerar pending", command)


def action_generate_final() -> None:
    title("Gerar outputs finais da app")
    print("Este passo le app-catalog-final.json e gera app-catalog.json, stats e Excel.")

    country = ask_text("Pais (para sugerir caminho default)", "India")
    final_input_path = ask_text("Input app-catalog-final.json", default_final_input_path(country))

    command = [
        sys.executable,
        "-m",
        "scripts.generate_resume_json",
        "--final-input",
        final_input_path,
    ]
    run_step("Gerar outputs finais", command)


def action_import_base44() -> None:
    title("Importar para Base44")
    print("Este passo envia app-catalog.json para a entidade Coin na Base44.")
    print("Escolhe modo com cuidado para evitar escrita indevida.")

    country = ask_text("Pais (para sugerir caminho default)", "India")
    input_path = ask_text("Input app-catalog.json", default_app_catalog_path(country))

    command = [sys.executable, "-m", "scripts.import_base44_coins", "--input", input_path]

    continent = ask_text("Continente (opcional: Europa|America|Asia|Africa|Oceania)", "")
    if continent:
        command.extend(["--continent", continent])

    condition = ask_text("Condition default", "Nao Tenho")
    if condition:
        command.extend(["--condition", condition])

    limit = ask_int("Limitar aos primeiros N (0 = sem limite)", default=0, allow_empty=False)
    if limit and limit > 0:
        command.extend(["--limit", str(limit)])

    batch_size = ask_int("Batch size", default=10, allow_empty=False)
    request_delay = ask_float("Request delay (segundos)", default=3.0)
    rate_limit_delay = ask_float("Rate limit delay (segundos)", default=60.0)
    max_retries = ask_int("Max retries", default=6, allow_empty=False)
    command.extend(
        [
            "--batch-size",
            str(batch_size),
            "--request-delay",
            str(request_delay),
            "--rate-limit-delay",
            str(rate_limit_delay),
            "--max-retries",
            str(max_retries),
        ]
    )

    print("Modo de execucao:")
    print("1) Dry run (so validar, nao escreve)")
    print("2) Create only")
    print("3) Create only + missing only")
    print("4) Replace (apaga do pais e recria)")
    mode = ask_text("Escolha", "1")

    if mode == "1":
        command.append("--dry-run")
    elif mode == "2":
        command.append("--create-only")
    elif mode == "3":
        command.extend(["--create-only", "--missing-only"])
    elif mode == "4":
        command.append("--replace")
    else:
        print("Modo invalido. Cancelado.")
        return

    run_step("Import Base44", command)


def menu() -> None:
    while True:
        title("uCoin to MySite - Menu")
        print("1) Ver guia de pre-requisitos")
        print("2) Pipeline completo")
        print("3) Scrape apenas (ucoin-catalog.json)")
        print("4) Gerar app-catalog-pending.json")
        print("5) Gerar outputs finais (app-catalog.json/stats/excel)")
        print("6) Importar para Base44")
        print("0) Sair")

        choice = ask_text("Escolhe uma opcao", "1")
        if choice == "1":
            explain_prerequisites()
        elif choice == "2":
            action_pipeline()
        elif choice == "3":
            action_scrape_only()
        elif choice == "4":
            action_generate_pending()
        elif choice == "5":
            action_generate_final()
        elif choice == "6":
            action_import_base44()
        elif choice == "0":
            print("A sair.")
            return
        else:
            print("Opcao invalida.")

        input("\nCarrega Enter para voltar ao menu...")


def main() -> int:
    menu()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
