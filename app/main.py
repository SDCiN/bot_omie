"""
Bot Omie - Main Orchestrator
=============================

Main script that coordinates:
1. Authentication (via auth.json cookies)
2. Navigation to Omie ERP
3. Report extraction loop
4. Excel processing
5. Database upsert
6. File archiving
"""

import os
import sys
import time
import logging
from datetime import datetime
from playwright.sync_api import sync_playwright, Page, Download
from dotenv import load_dotenv

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from auth import auth_exists, get_browser_context, AUTH_STATE_FILE, OMIE_URL, DOWNLOADS_DIR, realizar_login
from actions.process_excel.process_excel import process_excel
from actions.upsert_data.upsert_contas_a_pagar import upsert_data as upsert_contas_a_pagar
from actions.upsert_data.upsert_notas_faturadas import upsert_data as upsert_notas_faturadas
from actions.upsert_data.upsert_notas_debito import upsert_data as upsert_notas_debito

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('omie_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================
# REPORT CONFIGURATION MAPPING
# ============================================
# Note: Reports are accessed via:
# 1. Navigate to Finanças
# 2. Hover over getByRole('link', { name: 'paid' })
# 3. Click on the listitem with the creation date text
RELATORIOS = [
    {
        "nome_menu": "Contas a Pagar - PMO",
        "arquivo": "A PAGAR.xlsx",
        "tabela": "A_PAGAR",
        "data_slug": "rel-fin-custom-contas-a-pagar-pmo--5786318546",  # Unique identifier
        "upsert_handler": upsert_contas_a_pagar
    },
    {
        "nome_menu": "Notas Faturadas - PMO",
        "arquivo": "NF_FATURADAS.xlsx",
        "tabela": "NF_FATURADAS",
        "data_slug": "rel-fin-custom-notas-faturadas-pmo--5786322454",  # Unique identifier  
        "upsert_handler": upsert_notas_faturadas
    },
    {
        "nome_menu": "Notas Debito - PMO",
        "arquivo": "NOTAS_DEBITO.xlsx",
        "tabela": "NOTAS_DEBITO",
        "data_slug": "rel-fin-custom-notas-debito-pmo--5786322433",  # Unique identifier
        "upsert_handler": upsert_notas_debito
    },
]

# Constants
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries
REPORT_EXECUTION_TIMEOUT = 300  # 5 minutes max wait for report execution


def salvar_screenshot_debug(page: Page, etapa: str):
    """Salva um screenshot da página para diagnóstico de falhas de automação."""
    try:
        nome = f"debug_{etapa}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        caminho = os.path.join(DOWNLOADS_DIR, nome)
        page.screenshot(path=caminho)
        logger.info(f"📸 Screenshot de debug salvo em: {caminho}")
    except Exception as e:
        logger.warning(f"Não foi possível salvar screenshot de debug: {e}")


def fechar_popups(page: Page):
    """
    Checks for and closes any interrupting popups/modals.
    Common Omie popups:
    - Notifications ("Receba notificações...") -> Click "Depois"
    - Announcements -> Click "Fechar" or "x"
    """
    logger.info("Verificando popups...")
    try:
        # 1. Notification Popup ("Depois")
        # Try finding a button with text "Depois" which is common in Omie prompts
        depois_btn = page.get_by_text("Depois", exact=True).first
        if depois_btn.is_visible(timeout=2000):
            logger.info("Popup 'Receba notificações' detectado. Clicando em 'Depois'...")
            depois_btn.click()
            time.sleep(1)
            return

        # 2. Generic "Fechar" button
        fechar_btn = page.get_by_role("button", name="Fechar").first
        if fechar_btn.is_visible(timeout=1000):
            logger.info("Popup genérico detectado. Clicando em 'Fechar'...")
            fechar_btn.click()
            time.sleep(1)
            return

        # 3. "Agora não"
        agora_nao = page.get_by_text("Agora não", exact=True).first
        if agora_nao.is_visible(timeout=1000):
            logger.info("Popup detectado. Clicando em 'Agora não'...")
            agora_nao.click()
            time.sleep(1)
            return

    except Exception:
        # It's expected to fail if no popup exists, just ignore
        pass


def navegar_para_financas(page: Page) -> tuple[bool, Page]:
    """
    Navigate from Home to Finanças module.
    
    IMPORTANT: Clicking 'Acessar' opens a NEW TAB!
    
    Sequence:
    1. Click "Acessar" button - opens new tab
    2. Switch to new tab
    3. Click "Finanças" link
    4. Hover over "paid" icon
    
    Args:
        page: Playwright page object
    
    Returns:
        tuple: (success: bool, new_page: Page) - Returns the new page to use
    """
    try:
        logger.info("Navegando para Home...")
        
        # Navigate with 5 minute timeout
        try:
            page.goto(OMIE_URL, timeout=300000, wait_until='domcontentloaded')
            logger.info(f"Página carregada: {page.url}")
        except Exception as e:
            logger.error(f"Erro ao navegar para {OMIE_URL}: {e}")
            return False, page
        
        time.sleep(5)
        
        # Check if we're on the login page
        if "login" in page.url.lower():
            logger.warning("Redirecionado para login! Tentando login automático...")
            if not realizar_login(page):
                 return False, page
        
        # Click "Acessar" button - THIS OPENS A NEW TAB!
        logger.info("Procurando botão 'Acessar'...")
        try:
            acessar_button = None
            
            # Helper function to find button
            def encontrar_acessar_btn(timeout=5000):
                btn = None
                try:
                    # 1. Try main content first (Correct Selector)
                    # User reported specific issue where banner button is clicked instead
                    btn = page.get_by_role("main").get_by_role("button", name="Acessar").first
                    if btn.is_visible(timeout=timeout):
                        return btn

                    # 2. Try role=button (Fallback)
                    btn = page.get_by_role("button", name="Acessar").first
                    if btn.is_visible(timeout=timeout):
                        # Verify if it's not the banner one if possible, or just return it as fallback
                        # Log warning that we are using fallback
                        logger.warning("Usando seletor genérico para botão Acessar (pode ser o do banner)")
                        return btn
                    
                    # 3. Try text match (fallback)
                    btn = page.get_by_text("Acessar", exact=True).first
                    if btn.is_visible(timeout=timeout):
                        logger.info("Botão 'Acessar' encontrado via texto.")
                        return btn
                except:
                    pass
                return None

            # Tenta encontrar o botão com timeout estendido (2 min)
            # para dar chance de carregamento lento
            logger.info("Aguardando botão 'Acessar' por até 2 minutos...")
            
            start_time = time.time()
            found = False
            
            # Polling loop for 2 minutes or until found
            while time.time() - start_time < 120:
                acessar_button = encontrar_acessar_btn(timeout=1000)
                if acessar_button:
                    found = True
                    break
                time.sleep(2)
            
            if not found:
                # Timeout occurred - Assume we need to login
                logger.warning("Botão 'Acessar' não encontrado em 2 minutos. Tentando login automático...")
                
                # Force navigation to login if needed/check login
                if realizar_login(page):
                     # Se o login funcionou, tenta achar o botão de novo
                     logger.info("Login realizado. Procurando botão 'Acessar' novamente...")
                     
                     # Wait up to 60s for it to appear after login
                     found_after_login = False
                     start_time_login = time.time()
                     while time.time() - start_time_login < 60:
                        acessar_button = encontrar_acessar_btn(timeout=1000)
                        if acessar_button:
                            found_after_login = True
                            break
                        time.sleep(1)
                     
                     if not found_after_login:
                        logger.error("Falha: Botão 'Acessar' não apareceu após login.")
                        return False, page
                else:
                     logger.error("Falha no login automático.")
                     return False, page

            if not acessar_button:
                 logger.error("Botão 'Acessar' não pode ser localizado.")
                 return False, page
            
            logger.info("Clicando em 'Acessar' (abrirá nova aba)...")
            
            # Get context to listen for new page
            context = page.context
            
            # Click and wait for new page
            with context.expect_page(timeout=300000) as new_page_info:
                acessar_button.click()
            
            # Switch to the new page
            new_page = new_page_info.value
            logger.info(f"✅ Nova aba aberta: {new_page.url}")
            
            # Wait for new page to load
            new_page.wait_for_load_state('domcontentloaded', timeout=300000)
            time.sleep(5)
            
            # From now on, work with the NEW page
            page = new_page
            logger.info(f"Trabalhando na nova aba: {page.url}")
                
        except Exception as e:
            logger.error(f"Erro ao clicar em 'Acessar' ou trocar de aba: {e}")
            return False, page
        
        # Click "Finanças" in the NEW tab
        logger.info("Aguardando 'Finanças' carregar na nova aba...")
        time.sleep(5)
        
        logger.info("Procurando link 'Finanças'...")
        try:
            financas_found = False
            
            # Try direct link
            try:
                page.get_by_role("link", name="Finanças").wait_for(state="visible", timeout=300000)
                financas_link = page.get_by_role("link", name="Finanças")
                financas_found = True
                logger.info("✅ Link 'Finanças' encontrado")
            except:
                pass
            
            # Try text search
            if not financas_found:
                try:
                    financas_link = page.get_by_text("Finanças", exact=False).first
                    financas_link.wait_for(state="visible", timeout=60000)
                    financas_found = True
                    logger.info("✅ Link 'Finanças' encontrado via texto")
                except:
                    pass
            
            if not financas_found:
                logger.error("❌ Link 'Finanças' não encontrado")
                logger.error(f"URL: {page.url}")
                return False, page
            
            logger.info("Clicando em 'Finanças'...")
            financas_link.click()
            time.sleep(5)
        except Exception as e:
            logger.error(f"Erro ao clicar em 'Finanças': {e}")
            return False, page
        
        # Hover over "paid"
        logger.info("Passando mouse sobre 'paid'...")
        try:
            page.get_by_role("link", name="paid").wait_for(state="visible", timeout=300000)
            page.get_by_role("link", name="paid").hover()
            time.sleep(3)
        except Exception as e:
            logger.error(f"Erro ao passar mouse em 'paid': {e}")
            salvar_screenshot_debug(page, "hover_paid_navegacao")
            return False, page
        
        logger.info("✅ Navegação para Finanças concluída")
        return True, page  # Return the NEW page!
        
    except Exception as e:
        logger.error(f"❌ Erro na navegação: {e}")
        return False, page
        
        time.sleep(5)
        
        # Check if we're on the login page
        if "login" in page.url.lower():
            logger.error("❌ Redirecionado para login! Cookies expiraram.")
            logger.error("Execute 'Primeira Configuração' novamente.")
            return False
        
        # Click "Acessar" button - THIS OPENS A NEW TAB!
        logger.info("Procurando botão 'Acessar'...")
        try:
            # Try main content first
            try:
                acessar_button = page.get_by_role("main").get_by_role("button", name="Acessar")
                acessar_button.wait_for(state="visible", timeout=300000)
            except:
                acessar_button = page.get_by_role("button", name="Acessar").first
                acessar_button.wait_for(state="visible", timeout=300000)
            
            logger.info("Clicando em 'Acessar' (abrirá nova aba)...")
            
            # Get context to listen for new page
            context = page.context
            
            # Click and wait for new page
            with context.expect_page(timeout=300000) as new_page_info:
                acessar_button.click()
            
            # Switch to the new page
            new_page = new_page_info.value
            logger.info(f"✅ Nova aba aberta: {new_page.url}")
            
            # Wait for new page to load
            new_page.wait_for_load_state('domcontentloaded', timeout=300000)
            time.sleep(5)
            
            # From now on, work with the NEW page
            page = new_page
            logger.info(f"Trabalhando na nova aba: {page.url}")
                
        except Exception as e:
            logger.error(f"Erro ao clicar em 'Acessar' ou trocar de aba: {e}")
            return False
        
        # Click "Finanças" in the NEW tab
        logger.info("Aguardando 'Finanças' carregar na nova aba...")
        time.sleep(5)
        
        logger.info("Procurando link 'Finanças'...")
        try:
            financas_found = False
            
            # Try direct link
            try:
                page.get_by_role("link", name="Finanças").wait_for(state="visible", timeout=300000)
                financas_link = page.get_by_role("link", name="Finanças")
                financas_found = True
                logger.info("✅ Link 'Finanças' encontrado")
            except:
                pass
            
            # Try text search
            if not financas_found:
                try:
                    financas_link = page.get_by_text("Finanças", exact=False).first
                    financas_link.wait_for(state="visible", timeout=60000)
                    financas_found = True
                    logger.info("✅ Link 'Finanças' encontrado via texto")
                except:
                    pass
            
            if not financas_found:
                logger.error("❌ Link 'Finanças' não encontrado")
                logger.error(f"URL: {page.url}")
                return False
            
            logger.info("Clicando em 'Finanças'...")
            financas_link.click()
            time.sleep(5)
        except Exception as e:
            logger.error(f"Erro ao clicar em 'Finanças': {e}")
            return False
        
        # Hover over "paid"
        logger.info("Passando mouse sobre 'paid'...")
        try:
            page.get_by_role("link", name="paid").wait_for(state="visible", timeout=300000)
            page.get_by_role("link", name="paid").hover()
            time.sleep(3)
        except Exception as e:
            logger.error(f"Erro ao passar mouse em 'paid': {e}")
            return False
        
        logger.info("✅ Navegação para Finanças concluída")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erro na navegação: {e}")
        return False


def extrair_relatorio_omie(page: Page, nome_menu: str, data_slug: str, nome_arquivo_destino: str) -> str:
    """
    Extracts a single report from Omie ERP using unique data-slug.
    
    Args:
        page: Playwright page object
        nome_menu: Report name for logging
        data_slug: Unique data-slug attribute value
        nome_arquivo_destino: Desired filename
    
    Returns:
        str: Path to downloaded file, or None if failed
    """
    try:
        logger.info(f"Extraindo relatório: {nome_menu}")

        # Use CSS selector with data-slug attribute (unique identifier)
        selector = f'[data-slug="{data_slug}"]'
        report_element = page.locator(selector)

        # IMPORTANT: Hover over 'paid' first to reveal the menu
        logger.info("Revelando menu de relatórios...")
        try:
            # O hover da navegação inicial pode ter deixado o menu aberto;
            # nesse estado o link 'paid' fica oculto e o hover falharia.
            if report_element.is_visible():
                logger.info("Menu de relatórios já está aberto.")
            else:
                fechar_popups(page)
                try:
                    page.get_by_role("link", name="paid").hover(timeout=15000)
                except Exception:
                    # Fallback: aciona o módulo Finanças direto pelo data-id
                    logger.warning("Hover em 'paid' falhou. Acionando módulo FIN diretamente...")
                    page.locator('a[data-id="FIN"]').first.click(timeout=15000, force=True)
                time.sleep(2)
            logger.info("✅ Menu revelado")
        except Exception as e:
            logger.error(f"Erro ao revelar menu: {e}")
            salvar_screenshot_debug(page, "revelar_menu")
            return None

        # Phase 1: Click on report using unique data-slug
        logger.info(f"Selecionando relatório usando data-slug...")
        try:
            report_element.wait_for(state="visible", timeout=60000)
            report_element.click()
            logger.info(f"✅ Relatório clicado")
        except Exception as e:
            logger.error(f"❌ Erro ao clicar no relatório: {e}")
            logger.error(f"Data-slug: '{data_slug}'")
            salvar_screenshot_debug(page, "clicar_relatorio")
            return None
        
        time.sleep(3)
        
        # Phase 2: Click "Executar" button
        logger.info("Clicando em 'Executar'...")
        page.get_by_role("button", name=" Executar").click()
        time.sleep(2)
        
        # Phase 3: Wait for report execution (FIXED 5 MINUTES)
        logger.info("Aguardando execução do relatório (espera fixa de 5 minutos)...")
        # User requested explicit 5 minute wait instead of checking for selectors
        # This ensures the "Exportar" button is truly ready
        time.sleep(300)
        logger.info("Tempo de espera de 5 minutos concluído.")
        
        # Small buffer after long wait
        time.sleep(5)
        
        # Phase 4: Hover over "Exportar" menu item
        # O relatório pode demorar mais que a espera fixa de 5 minutos: o item
        # 'Exportar' existe no DOM mas fica oculto até a execução terminar.
        # Aguarda ele ficar visível por até 5 minutos extras antes do hover.
        fechar_popups(page)
        logger.info("Aguardando menu 'Exportar' ficar visível...")
        exportar_item = page.get_by_role("menuitem", name="Exportar")
        exportar_item.wait_for(state="visible", timeout=300000)
        logger.info("Passando mouse sobre 'Exportar'...")
        exportar_item.hover()
        time.sleep(1)
        
        # Phase 5: Click "Excel" format and wait for download
        logger.info("Selecionando formato Excel...")
        
        with page.expect_download(timeout=300000) as download_info:
            page.get_by_role("menuitem", name="Excel").locator("span").first.click()
        
        download: Download = download_info.value
        
        # Get original filename from download
        original_filename = download.suggested_filename
        
        # Save with desired filename (renaming automatically)
        dest_path = os.path.join(DOWNLOADS_DIR, nome_arquivo_destino)
        download.save_as(dest_path)
        
        logger.info(f"✅ Arquivo baixado e renomeado:")
        logger.info(f"   Original: {original_filename}")
        logger.info(f"   Salvo como: {nome_arquivo_destino}")
        logger.info(f"   Caminho: {dest_path}")

        
        # Small delay before returning
        time.sleep(2)
        
        # Return to the reports menu by hovering over 'paid' again
        logger.info("Retornando ao menu de relatórios...")
        try:
            fechar_popups(page)
            page.get_by_role("link", name="paid").hover()
            time.sleep(1)
        except Exception as e:
            logger.warning(f"⚠️ Aviso ao retornar ao menu (o download foi bem sucedido): {e}")
            # We don't return None here because the download WAS successful
        
        return dest_path
        
    except Exception as e:
        logger.error(f"❌ Erro ao extrair relatório '{nome_menu}': {e}")
        salvar_screenshot_debug(page, "extrair_relatorio")
        return None


def processar_e_salvar(arquivo_path: str, tabela: str, upsert_handler) -> bool:
    """
    Process Excel file and save to database.
    
    Args:
        arquivo_path: Path to downloaded Excel file
        tabela: Target table name
        upsert_handler: Function to handle database upsert
    
    Returns:
        bool: True if successful
    """
    try:
        logger.info(f"Processando arquivo: {arquivo_path}")
        
        # Process Excel
        df = process_excel(arquivo_path)
        
        if df.empty:
            logger.warning(f"DataFrame vazio para {tabela}")
            return False
        
        logger.info(f"Processados {len(df)} registros para {tabela}")
        
        # Upsert to database
        rows_affected = upsert_handler(df, arquivo_path)
        logger.info(f"Upsert concluído: {rows_affected} linhas afetadas em {tabela}")
        
        return True
        
    except Exception as e:
        logger.error(f"Erro ao processar/salvar {tabela}: {e}")
        return False


def run_extraction(relatorios_selecionados: list = None):
    """
    Main extraction function that processes all reports.
    
    Args:
        relatorios_selecionados: List of report configs to process.
                                If None, processes all reports.
    """
    if relatorios_selecionados is None:
        relatorios_selecionados = RELATORIOS
    
    # Check authentication
    if not auth_exists():
        logger.error("Autenticação não encontrada. Execute a primeira configuração.")
        raise Exception("auth.json não encontrado. Execute primeira_configuracao() primeiro.")
    
    logger.info("="*60)
    logger.info("INICIANDO EXTRAÇÃO DE RELATÓRIOS OMIE")
    logger.info(f"Total de relatórios: {len(relatorios_selecionados)}")
    logger.info("="*60)
    
    with sync_playwright() as playwright:
        browser, context = get_browser_context(playwright, headless=True)
        page = context.new_page()
        
        try:
            # Navigate to Finanças and get the NEW page
            success, page = navegar_para_financas(page)
            if not success:
                raise Exception("Falha na navegação para Finanças")
            
            logger.info(f"✅ Usando página: {page.url}")
            
            # Process each report
            successful = 0
            failed = 0
            
            for rel in relatorios_selecionados:
                nome_menu = rel.get("nome_menu") or rel.get("nome")
                arquivo = rel.get("arquivo")
                tabela = rel.get("tabela")
                data_slug = rel.get("data_slug")  # Get the unique data-slug
                upsert_handler = rel.get("upsert_handler")
                
                # If upsert_handler not in dict (coming from GUI), get it
                if upsert_handler is None or data_slug is None:
                    for r in RELATORIOS:
                        if r["tabela"] == tabela:
                            upsert_handler = r["upsert_handler"]
                            data_slug = r.get("data_slug")
                            break
                
                logger.info("-"*40)
                logger.info(f"Processando: {nome_menu}")
                
                # Retry logic
                for attempt in range(MAX_RETRIES):
                    try:
                        # Extract report - now passing the data_slug parameter
                        arquivo_path = extrair_relatorio_omie(page, nome_menu, data_slug, arquivo)
                        
                        if arquivo_path and os.path.exists(arquivo_path):
                            # Process and save
                            if processar_e_salvar(arquivo_path, tabela, upsert_handler):
                                successful += 1
                                break
                        
                        logger.warning(f"Tentativa {attempt + 1}/{MAX_RETRIES} falhou para {nome_menu}")
                        time.sleep(RETRY_DELAY)
                        
                    except Exception as e:
                        logger.error(f"Erro na tentativa {attempt + 1}: {e}")
                        time.sleep(RETRY_DELAY)
                else:
                    logger.error(f"Falha após {MAX_RETRIES} tentativas: {nome_menu}")
                    failed += 1
                
                # Small delay between reports
                time.sleep(3)
            
            logger.info("="*60)
            logger.info("EXTRAÇÃO CONCLUÍDA")
            logger.info(f"Sucesso: {successful} | Falha: {failed}")
            logger.info("="*60)
            
        except Exception as e:
            logger.error(f"Erro crítico na extração: {e}")
            raise
        finally:
            browser.close()


def run_once():
    """
    Convenience function to run extraction once with all reports.
    """
    run_extraction()


if __name__ == "__main__":
    # Run extraction when called directly
    run_once()
    
    if os.getenv("AUTO_CLOSE", "false").lower() == "true":
        logger.info("AUTO_CLOSE ativado. Finalizando execução.")
        sys.exit(0)
