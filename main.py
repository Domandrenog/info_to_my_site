#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from difflib import get_close_matches
from pathlib import Path
from types import SimpleNamespace

from scripts import fix_site_issues_api
from scripts.catalog_paths import (
    CATALOG_ROOT,
    continent_label_for_country,
    country_directory,
    normalize_continent,
    slugify,
)
from scripts.plan_missing_country_tracking import (
    build_collection_command,
    load_tracking_plans,
    print_tracking_plans,
    select_tracking_plans,
)
from scripts.recheck_ucoin_photos import load_missing_photo_entries

PROJECT_DIR = Path(__file__).resolve().parent


def line() -> None:
    print("-" * 72)


def title(text: str) -> None:
    os.system("cls" if os.name == "nt" else "clear")
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


def run_step(
    step_name: str,
    command: list[str],
    *,
    accepted_exit_codes: set[int] | None = None,
) -> int | None:
    title(f"Executar: {step_name}")
    print("Comando:")
    print(" ".join(command))
    if not ask_yes_no("Queres executar agora?", default=True):
        print("Execucao cancelada pelo utilizador.")
        return None

    accepted = accepted_exit_codes or {0}
    try:
        result = subprocess.run(command, cwd=PROJECT_DIR, check=False)
        if result.returncode not in accepted:
            print(f"Falhou com codigo de saida {result.returncode}.")
            return result.returncode
        if result.returncode == 1:
            print("Concluido: foram encontradas diferencas.")
        else:
            print("Concluido com sucesso.")
        return result.returncode
    except subprocess.CalledProcessError as exc:
        # Compatibilidade com mocks/callers que ainda possam levantar este erro.
        if exc.returncode in accepted:
            print("Concluido: foram encontradas diferencas.")
            return exc.returncode
        print(f"Falhou com codigo de saida {exc.returncode}.")
        return exc.returncode


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
    print("   Importar e preparar catálogos do uCoin > Importar um país — processo completo")


def default_catalog_path(country: str, continent: str = "") -> str:
    return str(country_directory(country, continent) / "ucoin-catalog.json")


def default_app_catalog_path(country: str, continent: str = "") -> str:
    return str(country_directory(country, continent) / "app-catalog.json")


def suggest_app_catalog_paths(input_path: str) -> list[Path]:
    """Return nearby local app catalogue paths for a misspelled country folder."""
    catalogues = sorted((PROJECT_DIR / CATALOG_ROOT).glob("*/*/app-catalog.json"))
    requested_folder = Path(input_path).parent.name
    matches = get_close_matches(requested_folder, [path.parent.name for path in catalogues], n=3, cutoff=0.6)
    return [path for path in catalogues if path.parent.name in matches]


def default_final_input_path(country: str, continent: str = "") -> str:
    return str(country_directory(country, continent) / "app-catalog-final.json")


def ask_country_link_name() -> str:
    print("Se o link do uCoin usa um nome diferente do pais, podes indicar aqui.")
    if not ask_yes_no("O nome do link e diferente?", default=False):
        return ""
    return ask_text("Nome do link no uCoin (ex: belarus)", "")


def ask_continent(country: str) -> str:
    default = continent_label_for_country(country)
    while True:
        continent = ask_text("Continente (Europa|America|Asia|Africa|Oceania)", default)
        try:
            normalize_continent(continent)
            return continent
        except ValueError as exc:
            print(exc)


def add_browser_mode(command: list[str]) -> None:
    print("Browser para uCoin:")
    print("  1) Ligar ao Chromium aberto com remote debugging (porta 9222)")
    print("  2) Abrir automaticamente Chromium em modo incognito")
    while True:
        mode = ask_text("Escolha", "1")
        if mode == "1":
            command.extend(["--attach-cdp", "--cdp-url", "http://127.0.0.1:9222", "--no-manual-session"])
            return
        if mode == "2":
            command.extend(["--incognito", "--cdp-url", "http://127.0.0.1:9222", "--manual-session"])
            return
        print("Escolhe 1 ou 2.")


def pending_rarity_catalogues() -> list[Path]:
    return sorted(
        path
        for path in (PROJECT_DIR / CATALOG_ROOT).glob("*/*/app-catalog-pending.json")
        if not path.with_name("app-catalog.json").is_file()
    )


def complete_pending_rarities() -> bool:
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.manage_pending_rarities",
                "--wait-for-final",
                "--cleanup-intermediate",
            ],
            cwd=PROJECT_DIR,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Não foi possível concluir as raridades: código {exc.returncode}.")
        return False
    print("Catálogos finais gerados; ficheiros intermédios removidos.")
    return True


def action_pipeline() -> None:
    title("Importar um país — processo completo")
    print("Executa scrape, pending/final, outputs finais e prepara a importacao Base44.")

    country = ask_text("Pais (ex: India, Canada)")
    if not country:
        print("Pais obrigatorio.")
        return

    continent = ask_continent(country)

    command = [sys.executable, "-m", "scripts.ucoin_pipeline", country, "--continent", continent]

    country_link_name = ask_country_link_name()
    if country_link_name:
        command.extend(["--country-link-name", country_link_name])

    start_year = ask_int("Start year (opcional)", default=None, allow_empty=True)
    if start_year is not None:
        command.extend(["--start-year", str(start_year)])

    add_browser_mode(command)

    if ask_yes_no("Apagar ficheiros intermédios e deixar só os outputs finais?", default=True):
        command.append("--cleanup-intermediate")

    if run_step("Importar um país — processo completo", command) == 0:
        action_import_base44(
            country=country,
            continent=continent,
            input_path=default_app_catalog_path(country, continent),
        )


def action_collect_country_pending() -> None:
    title("Recolher dados do uCoin e criar catálogo pendente")
    print("Recolhe o catálogo técnico e prepara o app-catalog-pending.json para definir raridades.")

    country = ask_text("Pais (ex: India, Canada)")
    if not country:
        print("Pais obrigatorio.")
        return

    continent = ask_continent(country)

    command = [
        sys.executable,
        "-m",
        "scripts.ucoin_pipeline",
        country,
        "--continent",
        continent,
        "--no-wait-for-final",
    ]

    country_link_name = ask_country_link_name()
    if country_link_name:
        command.extend(["--country-link-name", country_link_name])

    start_year = ask_int("Start year (opcional)", default=None, allow_empty=True)
    if start_year is not None:
        command.extend(["--start-year", str(start_year)])

    add_browser_mode(command)

    run_step("Recolher dados e criar catálogo pendente", command)


def action_collect_missing_country_tracking() -> None:
    title("Adicionar países do Site Base44 sem catálogo local")
    print("Consulta a Base44, recolhe os catálogos em falta e conclui a classificação das raridades.")
    print("No fim gera os outputs finais e limpa os ficheiros intermédios de cada país.")
    print("Este fluxo não escreve nem apaga registos na Base44.\n")

    plans, error = load_tracking_plans(PROJECT_DIR / CATALOG_ROOT, "app-catalog.json")
    if plans is None:
        print(f"Não foi possível consultar a Base44: {error}")
        return
    if not plans:
        pending = pending_rarity_catalogues()
        if pending:
            print(f"Não há países novos para recolher; existem {len(pending)} catálogos a aguardar raridade.")
            complete_pending_rarities()
        else:
            print("Todos os países do Site Base44 já têm catálogo final.")
        return

    print_tracking_plans(plans)

    while True:
        selection = ask_text("Países a recolher (todos ou números separados por vírgula)", "todos")
        try:
            selected_plans = select_tracking_plans(plans, selection)
            break
        except ValueError as exc:
            print(exc)

    browser_args: list[str] = []
    add_browser_mode(browser_args)
    commands = [
        build_collection_command(plan, browser_args=browser_args)
        for plan in selected_plans
    ]

    print("\nPlano final:")
    for index, (plan, command) in enumerate(zip(selected_plans, commands), start=1):
        print(f"{index}/{len(commands)} — {plan['country']}")
        print(" ".join(command))

    print("\nSerão criados ucoin-catalog.json e app-catalog-pending.json para cada país.")
    print("Cada recolha começa no primeiro ano já coberto pelo Site Base44, para não omitir séries em continuação.")
    print("Nenhuma moeda será importada para a Base44 nesta etapa.")
    if not ask_yes_no("Confirmas a recolha de todos os países selecionados?", default=False):
        print("Recolha cancelada; nenhuma alteração efetuada.")
        return

    for index, (plan, command) in enumerate(zip(selected_plans, commands), start=1):
        print(f"\n[{index}/{len(commands)}] A recolher {plan['country']}...", flush=True)
        try:
            subprocess.run(command, cwd=PROJECT_DIR, check=True)
        except subprocess.CalledProcessError as exc:
            print(f"A recolha de {plan['country']} falhou com código {exc.returncode}.")
            print("O lote foi interrompido para não avançar sem rever o erro.")
            return

    print(f"\nRecolha concluída para {len(selected_plans)} países.")
    print("Os países recolhidos ficam marcados como 'ainda sem raridade'.")
    print("A preparar o lote único para classificação de raridades...")
    complete_pending_rarities()


def action_finalize_pending_catalogues(country: str = "") -> None:
    title("Definir raridades e gerar outputs finais")
    command = [
        sys.executable,
        "-m",
        "scripts.manage_pending_rarities",
        "--wait-for-final",
    ]
    if country:
        command.extend(["--country", country])
        print(f"Será preparado apenas o país: {country}.")
    else:
        print("Serão preparados todos os países que ainda aguardam raridade.")
    print("Depois da classificação serão gerados app-catalog.json, estatísticas e Excel.")
    if ask_yes_no("Apagar os ficheiros intermédios no fim?", default=True):
        command.append("--cleanup-intermediate")
    run_step("Definir raridades e gerar outputs finais", command)


def menu_finalize_pending_catalogues() -> None:
    while True:
        title("Definir raridades e gerar outputs finais")
        print("1) Todos os países pendentes")
        print("2) Um país específico")
        print("3) Voltar")

        choice = ask_text("Escolhe uma opção", "1")
        if choice == "1":
            action_finalize_pending_catalogues()
        elif choice == "2":
            country = ask_text("País")
            if not country:
                print("País obrigatório.")
            else:
                action_finalize_pending_catalogues(slugify(country))
        elif choice == "3":
            return
        else:
            print("Opção inválida.")

        input("\nCarrega Enter para continuar...")


def action_generate_pending() -> None:
    title("Gerar app-catalog-pending.json")
    print("Este passo transforma ucoin-catalog.json no formato simplificado para classificacao.")

    country = ask_text("Pais (para sugerir caminho default)", "India")
    continent = ask_continent(country)
    default_input = default_catalog_path(country, continent)
    input_path = ask_text("Input ucoin-catalog.json", default_input)

    command = [sys.executable, "-m", "scripts.generate_resume_json", "--input", input_path]
    if ask_yes_no("Esperar por app-catalog-final.json no fim?", default=True):
        command.append("--wait-for-final")

    run_step("Gerar pending", command)


def action_generate_final() -> None:
    title("Gerar outputs finais da app")
    print("Este passo le app-catalog-final.json e gera app-catalog.json, stats e Excel.")

    country = ask_text("Pais (para sugerir caminho default)", "India")
    continent = ask_continent(country)
    final_input_path = ask_text("Input app-catalog-final.json", default_final_input_path(country, continent))
    final_input = Path(final_input_path)

    recovering_from_app_catalogue = False
    if not final_input.exists():
        app_catalogue = final_input.with_name("app-catalog.json")
        if app_catalogue.exists():
            print(f"Ficheiro final nao encontrado: {final_input}")
            print(f"A regenerar stats e Excel a partir de: {app_catalogue}")
            final_input = app_catalogue
            final_input_path = str(app_catalogue)
            recovering_from_app_catalogue = True
        else:
            print(f"Ficheiro nao encontrado: {final_input}")
            available = sorted(CATALOG_ROOT.glob("*/*/app-catalog-final.json"))
            if available:
                print("Ficheiros final disponiveis:")
                for path in available:
                    print(f"- {path}")
            print("Dica: primeiro gera pending e preenche o app-catalog-final.json desse pais.")
            return

    if final_input.stat().st_size == 0:
        print(f"Ficheiro vazio: {final_input}")
        print("Preenche app-catalog-final.json com o resultado final baseado no app-catalog-pending.json.")
        return

    try:
        json.loads(final_input.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"JSON invalido em {final_input}: linha {exc.lineno}, coluna {exc.colno}.")
        print("Corrige o JSON antes de gerar os outputs finais.")
        return

    command = [
        sys.executable,
        "-m",
        "scripts.generate_resume_json",
        "--final-input",
        final_input_path,
    ]
    if not recovering_from_app_catalogue and ask_yes_no("Apagar ficheiros intermédios e deixar só os finais?", default=True):
        command.append("--cleanup-intermediate")
    run_step("Gerar outputs finais", command)


def action_import_base44(
    *,
    country: str | None = None,
    continent: str | None = None,
    input_path: str | None = None,
) -> None:
    title("Importar para Base44")
    print("Este passo envia app-catalog.json para a entidade Coin na Base44.")
    print("Escolhe modo com cuidado para evitar escrita indevida.")

    if country is None:
        country = ask_text("Pais (para sugerir caminho default)", "India")
    if continent is None:
        continent = ask_continent(country)
    if input_path is None:
        input_path = ask_text("Input app-catalog.json", default_app_catalog_path(country, continent))
    if not Path(input_path).is_file():
        print(f"Ficheiro nao encontrado: {input_path}")
        suggestions = suggest_app_catalog_paths(input_path)
        if suggestions:
            print("Quiseste dizer:")
            for path in suggestions:
                print(f"- {path.relative_to(PROJECT_DIR)}")
        return

    command = [
        sys.executable,
        "-m",
        "scripts.import_base44_coins",
        "--input",
        input_path,
        "--continent",
        continent,
    ]

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


def action_check_differences() -> None:
    title("Verificar diferencas do site")
    print("Compara app-catalog com All_Coins e Site Base44 para listar inconsistencias por moeda.")

    country = ask_text("Pais (opcional, ex: bielorrussia; vazio = todos)", "")
    include_warnings = ask_yes_no("Incluir warnings no relatorio?", default=False)

    if country:
        country_slug = slugify(country)
        default_output = str(country_directory(country) / f"{country_slug}-differences.json")
    else:
        default_output = str(CATALOG_ROOT / "all-differences.json")
    save_report = ask_yes_no("Guardar ficheiro de diferencas em info/paises/?", default=True)
    output_path = ask_text("Ficheiro de output", default_output) if save_report else ""
    temporary_output = False
    if output_path:
        report_path = Path(output_path)
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix="ucoin-differences-", suffix=".json")
        os.close(descriptor)
        report_path = Path(temporary_name)
        report_path.unlink(missing_ok=True)
        temporary_output = True

    previous_report_state = None
    if report_path.exists():
        stat = report_path.stat()
        previous_report_state = (stat.st_mtime_ns, stat.st_size)

    command = [
        sys.executable,
        "-m",
        "scripts.check_site_coin_differences",
        "--max-issues",
        str(999999),
    ]
    if country:
        command.extend(["--country", country])
    if include_warnings:
        command.append("--include-warnings")
    command.extend(["--output", str(report_path)])

    try:
        result = run_step("Check diferencas", command, accepted_exit_codes={0, 1})
        if result != 1:
            return

        current_report_state = None
        if report_path.exists():
            stat = report_path.stat()
            current_report_state = (stat.st_mtime_ns, stat.st_size)
        report_was_refreshed = current_report_state is not None and current_report_state != previous_report_state

        if report_was_refreshed:
            offer_difference_followups(report_path)
    finally:
        if temporary_output:
            report_path.unlink(missing_ok=True)


def unconfirmed_association_countries(report_path: Path) -> list[dict[str, object]]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    reports = payload if isinstance(payload, list) else [payload]
    countries: list[dict[str, object]] = []
    for report in reports:
        if not isinstance(report, dict) or report.get("error"):
            continue
        affected_coins = []
        for coin in report.get("coins_with_issues", []):
            if not isinstance(coin, dict):
                continue
            has_missing_association = any(
                isinstance(issue, dict) and issue.get("type") == "missing_api_coin_record"
                for issue in coin.get("issues", [])
            )
            if has_missing_association:
                affected_coins.append(coin)
        if affected_coins:
            countries.append(
                {
                    "country": str(report.get("country") or ""),
                    "country_name": str(report.get("country_name") or report.get("country") or ""),
                    "coin_count": len(affected_coins),
                }
            )
    return countries


def select_association_countries(countries: list[dict[str, object]]) -> list[dict[str, object]]:
    if len(countries) <= 1:
        return countries

    print("\nPaíses com associações por rever:")
    for index, item in enumerate(countries, start=1):
        count = int(item.get("coin_count", 0))
        coin_label = "moeda" if count == 1 else "moedas"
        print(f"{index}) {item.get('country_name', '')} — {count} {coin_label}")
    print("Escreve 'todos' ou os números separados por vírgulas.")

    while True:
        selection = ask_text("Países a rever", "todos").casefold()
        if selection in {"todos", "all"}:
            return countries
        try:
            indexes = [int(value.strip()) for value in selection.split(",") if value.strip()]
        except ValueError:
            indexes = []
        if indexes and all(1 <= index <= len(countries) for index in indexes):
            selected_indexes = set(indexes)
            return [item for index, item in enumerate(countries, start=1) if index in selected_indexes]
        print("Indica 'todos' ou números válidos da lista.")


def run_association_review(country: str) -> int:
    country_slug = slugify(country)
    command = [
        sys.executable,
        "-m",
        "scripts.fix_site_issues_api",
        "--country",
        country_slug,
        "--output",
        str(country_directory(country_slug) / f"{country_slug}-autofix-plan.json"),
        "--skip-create-missing",
        "--reconcile-missing-interactive",
        "--update-fields",
        "name",
        "years",
        "--apply",
    ]
    print("\n" + "#" * 72)
    print(f"REVER ASSOCIAÇÕES — {country.replace('-', ' ').title()}")
    print("#" * 72, flush=True)
    result = subprocess.run(command, cwd=PROJECT_DIR, check=False)
    if result.returncode:
        print(f"A revisão terminou com código {result.returncode}.")
    return result.returncode


def offer_unconfirmed_association_review(report_path: Path) -> None:
    try:
        countries = unconfirmed_association_countries(report_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"\nNão foi possível preparar a revisão das associações: {exc}")
        return
    if not countries:
        return

    total_coins = sum(int(item.get("coin_count", 0)) for item in countries)
    coin_label = "moeda" if total_coins == 1 else "moedas"
    country_label = "país" if len(countries) == 1 else "países"
    print("\nPróximo passo opcional:")
    print(
        f"1) Rever nomes semelhantes no Site Base44 — "
        f"{total_coins} {coin_label} em {len(countries)} {country_label}"
    )
    print("0) Ignorar por agora")
    while True:
        choice = ask_text("Escolhe uma opção", "0")
        if choice == "0":
            return
        if choice == "1":
            break
        print("Escolhe 1 ou 0.")

    review_unconfirmed_association_countries(countries)


def review_unconfirmed_association_countries(countries: list[dict[str, object]]) -> list[str]:
    selected_countries = select_association_countries(countries)
    reviewed_country_slugs: list[str] = []
    for item in selected_countries:
        country_slug = str(item.get("country") or "")
        reviewed_country_slugs.append(country_slug)
        run_association_review(country_slug)
    return reviewed_country_slugs


def offer_ucoin_photo_recheck(report_path: Path) -> None:
    try:
        entries = load_missing_photo_entries(report_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"\nNão foi possível preparar a revisão das fotografias: {exc}")
        return
    if not entries:
        return

    coin_label = "moeda" if len(entries) == 1 else "moedas"
    print("\nPróximo passo opcional:")
    print(f"1) Rever no uCoin {len(entries)} {coin_label} sem fotografias disponíveis")
    print("0) Terminar")
    while True:
        choice = ask_text("Escolhe uma opção", "0")
        if choice == "0":
            return
        if choice == "1":
            break
        print("Escolhe 1 ou 0.")

    run_ucoin_photo_recheck(report_path)


def run_ucoin_photo_recheck(report_path: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "scripts.recheck_ucoin_photos",
        "--input",
        str(report_path),
    ]
    add_browser_mode(command)
    run_step("Rever fotografias no uCoin", command)


def offer_difference_followups(report_path: Path) -> None:
    try:
        association_countries = unconfirmed_association_countries(report_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"\nNão foi possível preparar a revisão das associações: {exc}")
        association_countries = []
    try:
        photo_entries = load_missing_photo_entries(report_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"\nNão foi possível preparar a revisão das fotografias: {exc}")
        photo_entries = []

    while association_countries or photo_entries:
        actions: list[tuple[str, str]] = []
        if association_countries:
            total_coins = sum(int(item.get("coin_count", 0)) for item in association_countries)
            coin_label = "moeda" if total_coins == 1 else "moedas"
            country_label = "país" if len(association_countries) == 1 else "países"
            actions.append(
                (
                    "associations",
                    "Rever nomes semelhantes no Site Base44 — "
                    f"{total_coins} {coin_label} em {len(association_countries)} {country_label}",
                )
            )
        if photo_entries:
            coin_label = "moeda" if len(photo_entries) == 1 else "moedas"
            actions.append(
                (
                    "photos",
                    f"Rever fotografias indisponíveis no uCoin — {len(photo_entries)} {coin_label}",
                )
            )

        print("\nPróximos passos opcionais:")
        for index, (_, label) in enumerate(actions, start=1):
            print(f"{index}) {label}")
        print("0) Terminar")

        while True:
            choice = ask_text("Escolhe uma opção", "0")
            if choice == "0":
                return
            try:
                selected_index = int(choice) - 1
                if selected_index < 0 or selected_index >= len(actions):
                    raise IndexError
            except (ValueError, IndexError):
                print("Escolhe uma das opções apresentadas.")
                continue
            break

        action = actions[selected_index][0]
        if action == "associations":
            reviewed_country_slugs = set(
                review_unconfirmed_association_countries(association_countries)
            )
            association_countries = [
                item
                for item in association_countries
                if str(item.get("country") or "") not in reviewed_country_slugs
            ]
        else:
            run_ucoin_photo_recheck(report_path)
            photo_entries = []


def print_notes_status(plan: dict[str, object]) -> None:
    entries = [
        item
        for item in plan.get("notes_status", [])
        if isinstance(item, dict) and int(item.get("total", 0)) > 0
    ]
    if not entries:
        return

    state_order = {"none": 0, "partial": 1, "complete": 2}
    state_labels = {
        "none": "nenhuma",
        "partial": "parcial",
        "complete": "completa",
    }
    print("\nEstado atual de notes por país:")
    for item in sorted(
        entries,
        key=lambda value: (
            state_order.get(str(value.get("state") or ""), 3),
            str(value.get("country") or ""),
        ),
    ):
        state = str(item.get("state") or "")
        with_notes = int(item.get("with_notes", 0))
        total = int(item.get("total", 0))
        fixable = int(item.get("fixable_missing_notes", 0))
        fixable_text = f"; {fixable} correções seguras disponíveis" if fixable else ""
        print(
            f"- {item.get('country', '')}: {state_labels.get(state, state)} "
            f"({with_notes}/{total} moedas com notes{fixable_text})"
        )


def choose_notes_country_scope(plan: dict[str, object]) -> set[str] | None:
    candidates = [
        item
        for item in plan.get("notes_status", [])
        if isinstance(item, dict) and int(item.get("fixable_missing_notes", 0)) > 0
    ]
    if not candidates:
        return set()

    all_country_slugs = {str(item.get("country_slug") or "") for item in candidates}
    countries_without_notes = [item for item in candidates if item.get("state") == "none"]
    choices: list[tuple[str, set[str] | None]] = []
    if countries_without_notes:
        slugs = {str(item.get("country_slug") or "") for item in countries_without_notes}
        coin_count = sum(int(item.get("fixable_missing_notes", 0)) for item in countries_without_notes)
        choices.append(
            (
                "Apenas países sem nenhuma note e com correções seguras — "
                f"{len(slugs)} países, {coin_count} moedas",
                slugs,
            )
        )
    choices.append(
        (
            "Todas as notes em falta — "
            f"{len(all_country_slugs)} países, "
            f"{sum(int(item.get('fixable_missing_notes', 0)) for item in candidates)} moedas",
            all_country_slugs,
        )
    )
    choices.append(("Escolher países", None))

    print("\nOnde queres preencher notes?")
    for index, (label, _) in enumerate(choices, start=1):
        print(f"{index}) {label}")
    print("0) Cancelar")

    while True:
        choice = ask_text("Escolhe o âmbito de notes", "1")
        if choice == "0":
            return None
        try:
            selected_index = int(choice) - 1
            if selected_index < 0 or selected_index >= len(choices):
                raise IndexError
        except (ValueError, IndexError):
            print("Escolhe uma das opções apresentadas.")
            continue

        selected_slugs = choices[selected_index][1]
        if selected_slugs is not None:
            return selected_slugs

        print("\nPaíses com correções de notes disponíveis:")
        for index, item in enumerate(candidates, start=1):
            state_label = "nenhuma" if item.get("state") == "none" else "parcial"
            print(
                f"{index}) {item.get('country', '')} — {state_label}; "
                f"{item.get('fixable_missing_notes', 0)} moedas"
            )
        while True:
            selection = ask_text("Números dos países, separados por vírgulas", "")
            try:
                indexes = {int(value.strip()) for value in selection.split(",") if value.strip()}
            except ValueError:
                indexes = set()
            if not indexes or any(index < 1 or index > len(candidates) for index in indexes):
                print("Indica pelo menos um número válido da lista.")
                continue
            return {str(candidates[index - 1].get("country_slug") or "") for index in indexes}


def action_autofix_issues() -> None:
    title("Corrigir dados no Site Base44")
    print("Analisa todos os países e só permite alterar os campos escolhidos.")
    print("Neste fluxo não criamos registos nem alteramos fotografias.")
    print("A recolher o estado atual do Site Base44 e dos catálogos...", flush=True)

    try:
        plan = fix_site_issues_api.collect_global_fix_plan(
            PROJECT_DIR / CATALOG_ROOT,
            PROJECT_DIR.parent / "All_Coins",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Não foi possível preparar as correções: {exc}")
        return

    errors = plan.get("errors", [])
    if errors:
        print("\nPaíses que não foi possível analisar:")
        for item in errors:
            print(f"- {item.get('country', '')}: {item.get('error', '')}")
        print("\nOperação bloqueada: o diagnóstico global ficou incompleto.")
        return

    conflicts = plan.get("conflicts", [])
    if conflicts:
        print(f"\nConflitos excluídos das correções automáticas: {len(conflicts)}")
        print("O mesmo registo recebeu propostas diferentes; estes campos não serão alterados.")
        for item in conflicts:
            coins = ", ".join(
                f"{coin.get('denomination', '')} ({coin.get('issuePeriod', '')})"
                for coin in item.get("coins", [])
            )
            print(f"- {item.get('country', '')}: {item.get('field', '')} — {coins}")

    missing = plan.get("missing", [])
    if missing:
        print(f"\nEntradas sem associação confirmada no Site Base44: {len(missing)}")
        print("Estas entradas serão apenas mostradas; não serão criadas nem alteradas.")
        current_country = ""
        for item in sorted(
            missing,
            key=lambda value: (
                str(value.get("country") or ""),
                str(value.get("denomination") or ""),
                str(value.get("issuePeriod") or ""),
            ),
        ):
            country = str(item.get("country") or "")
            if country != current_country:
                print(f"\n{country}:")
                current_country = country
            years = str(item.get("issuePeriod") or "")
            years_text = f" ({years})" if years else ""
            print(f"- {item.get('denomination', '')}{years_text}")

    photo_issues = int(plan.get("manual_counts", {}).get("photos", 0))
    if photo_issues:
        print(f"\nFotografias a rever manualmente: {photo_issues} ocorrências")
        print("Não aparecem como opção porque este diagnóstico não permite uma correção segura no Site Base44.")

    counts = fix_site_issues_api.update_field_counts(plan)
    if not counts:
        print("\nNão existem correções automáticas disponíveis para os campos suportados.")
        return

    if "notes" in counts:
        print_notes_status(plan)

    field_labels = {
        "url_ucoin": "Corrigir URL do uCoin",
        "notes": "Preencher notes",
        "name": "Corrigir nome",
        "years": "Corrigir anos/período",
    }
    available_fields = [field for field in fix_site_issues_api.SAFE_UPDATE_FIELDS if field in counts]
    print("\nCorreções disponíveis:")
    for index, field in enumerate(available_fields, start=1):
        print(f"{index}) {field_labels[field]} — {counts[field]} moedas")
    if len(available_fields) > 1:
        print(f"{len(available_fields) + 1}) Todas as opções acima")
    print("0) Cancelar")

    while True:
        choice = ask_text("Escolhe o que queres corrigir", "0")
        if choice == "0":
            print("Operação cancelada. O Site Base44 não foi alterado.")
            return
        if len(available_fields) > 1 and choice == str(len(available_fields) + 1):
            selected_fields = available_fields
            break
        try:
            selected_index = int(choice) - 1
            if selected_index < 0 or selected_index >= len(available_fields):
                raise IndexError
            selected_fields = [available_fields[selected_index]]
            break
        except (ValueError, IndexError):
            print("Escolhe uma das opções apresentadas.")

    selected_plan = fix_site_issues_api.select_update_fields(plan, selected_fields)
    if "notes" in selected_fields:
        selected_note_countries = choose_notes_country_scope(selected_plan)
        if selected_note_countries is None:
            print("Operação cancelada. O Site Base44 não foi alterado.")
            return
        selected_plan = fix_site_issues_api.restrict_update_field_to_countries(
            selected_plan,
            "notes",
            selected_note_countries,
        )
    updates = selected_plan.get("updates", [])
    if not updates:
        print("Não existem alterações seguras dentro do âmbito escolhido. O Site Base44 não foi alterado.")
        return
    total_changes = sum(len(item.get("set", {})) for item in updates)
    print(f"\nAlterações propostas: {total_changes} campos em {len(updates)} moedas")

    current_country = ""
    for item in sorted(
        updates,
        key=lambda value: (
            str(value.get("country") or ""),
            str(value.get("denomination") or ""),
            str(value.get("issuePeriod") or ""),
        ),
    ):
        country = str(item.get("country") or "")
        if country != current_country:
            print(f"\n{country}:")
            current_country = country
        years = str(item.get("issuePeriod") or "")
        years_text = f" ({years})" if years else ""
        print(f"- {item.get('denomination', '')}{years_text}")
        current_values = item.get("current", {})
        for field, new_value in item.get("set", {}).items():
            old_value = str(current_values.get(field) or "(vazio)")
            print(f"  {field}: {old_value} -> {new_value}")

    if not ask_yes_no(
        f"Aplicar exatamente estes {total_changes} campos no Site Base44?",
        default=False,
    ):
        print("Operação cancelada. O Site Base44 não foi alterado.")
        return

    args = SimpleNamespace(
        request_delay=1.5,
        rate_limit_delay=30.0,
        max_retries=5,
        continent="",
        condition="Não Tenho",
    )
    try:
        result = fix_site_issues_api.apply_plan(
            args,
            "",
            {"updates": updates, "creates": []},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"A atualização parou: {exc}")
        return

    print(
        "\nAtualização concluída e verificada: "
        f"{result['updated']} moedas atualizadas; {result['skipped']} ignoradas."
    )


def menu_importar_ucoin() -> None:
    while True:
        title("Importar do uCoin")
        print("1) Importar um país do uCoin para o Site Base44")
        print("2) Recolher países do Site sem tracking local")
        print("3) Executar uma etapa específica")
        print("4) Voltar")

        choice = ask_text("Escolhe uma opcao", "1")
        if choice == "1":
            action_pipeline()
        elif choice == "2":
            action_collect_missing_country_tracking()
        elif choice == "3":
            menu_specific_stage()
            continue
        elif choice == "4":
            return
        else:
            print("Opcao invalida.")

        input("\nCarrega Enter para continuar...")


def menu_specific_stage() -> None:
    while True:
        title("Executar uma etapa específica")
        print("1) Recolher dados do uCoin e criar catálogo pendente")
        print("2) Definir raridades e gerar outputs finais")
        print("3) Importar dados para o Site Base44")
        print("4) Voltar")

        choice = ask_text("Escolhe uma opcao", "1")
        if choice == "1":
            action_collect_country_pending()
        elif choice == "2":
            menu_finalize_pending_catalogues()
            continue
        elif choice == "3":
            action_import_base44()
        elif choice == "4":
            return
        else:
            print("Opcao invalida.")

        input("\nCarrega Enter para continuar...")


def menu_atualizar_site() -> None:
    while True:
        title("Atualizar Site Base44")
        print("1) Verificar diferenças: Site Base44 vs info local e All_Coins")
        print("2) Corrigir dados no Site Base44 (sem criar moedas novas)")
        print("3) Voltar")

        choice = ask_text("Escolhe uma opcao", "1")
        if choice == "1":
            action_check_differences()
        elif choice == "2":
            action_autofix_issues()
        elif choice == "3":
            return
        else:
            print("Opcao invalida.")

        input("\nCarrega Enter para continuar...")


def menu() -> None:
    while True:
        title("uCoin to MySite - Menu principal")
        print("1) Pré-requisitos")
        print("2) Importar do uCoin")
        print("3) Atualizar Site Base44")
        print("0) Sair")

        choice = ask_text("Escolhe uma opcao", "1")
        if choice == "1":
            explain_prerequisites()
        elif choice == "2":
            menu_importar_ucoin()
        elif choice == "3":
            menu_atualizar_site()
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
