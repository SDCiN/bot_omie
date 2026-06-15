"""
Bot Omie - Authentication Module
=================================

Handles authentication with Omie ERP using Playwright.
Features:
- First run: Opens visible browser for manual authentication (including 2FA)
- Subsequent runs: Uses saved cookies (auth.json) in headless mode
"""

import os
import time
import logging
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Paths
AUTH_STATE_FILE = os.path.join(os.path.dirname(__file__), '..', 'auth.json')
DOWNLOADS_DIR = os.path.join(os.path.dirname(__file__), 'downloads')

# Omie URL
OMIE_URL = os.getenv('OMIE_URL', 'https://app.omie.com.br')


def auth_exists() -> bool:
    """Check if authentication state file exists."""
    return os.path.exists(AUTH_STATE_FILE)


def get_browser_context(playwright, headless: bool = True) -> tuple[Browser, BrowserContext]:
    """
    Creates browser and context with proper configuration.
    
    Args:
        playwright: Playwright instance
        headless: Whether to run in headless mode
    
    Returns:
        tuple: (Browser, BrowserContext)
    """
    launch_args = []
    if not headless:
        # Firefox não tem flag de "iniciar maximizado"; define o tamanho da janela
        launch_args = ["-width", "1920", "-height", "1080"]

    browser = playwright.firefox.launch(
        headless=headless,
        slow_mo=100 if not headless else 0,  # Slow down for visibility on first run
        args=launch_args
    )

    context_options = {
        'accept_downloads': True,
        'permissions': [],  # Block all permissions (notifications, geo, etc)
    }

    if headless:
        # Headless não tem janela real: mantém viewport fixo
        context_options['viewport'] = {'width': 1920, 'height': 1080}
    else:
        # Sem viewport emulado: a página acompanha o tamanho real da janela
        context_options['no_viewport'] = True
    
    # Load saved state if exists (works for both headless and visible browser)
    if auth_exists():
        context_options['storage_state'] = AUTH_STATE_FILE
        logger.info(f"✅ Carregando cookies salvos de: {AUTH_STATE_FILE}")
    else:
        logger.warning(f"⚠️ Arquivo auth.json não encontrado. Execute 'Primeira Configuração'")
    
    context = browser.new_context(**context_options)
    
    # Ensure downloads directory exists
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    return browser, context


def save_auth_state(context: BrowserContext) -> None:
    """Save authentication state (cookies) to file."""
    context.storage_state(path=AUTH_STATE_FILE)
    logger.info(f"Authentication state saved to {AUTH_STATE_FILE}")


def primeira_configuracao() -> bool:
    """
    Runs the first-time setup with visible browser.
    User must authenticate manually (including 2FA if required).
    
    Returns:
        bool: True if authentication was successful and saved
    """
    logger.info("Iniciando primeira configuração com browser visível...")
    
    with sync_playwright() as playwright:
        browser, context = get_browser_context(playwright, headless=False)
        page = context.new_page()
        
        try:
            # Navigate to Omie login
            page.goto(OMIE_URL, timeout=60000)
            logger.info(f"Navegou para {OMIE_URL}")
            
            print("\n" + "="*60)
            print("PRIMEIRA CONFIGURAÇÃO - AUTENTICAÇÃO MANUAL")
            print("="*60)
            print("\n1. Faça login no Omie (incluindo 2FA se necessário)")
            print("2. Navegue até a página principal após login")
            print("3. Quando terminar, pressione ENTER neste terminal\n")
            
            input("Pressione ENTER quando terminar o login...")
            
            # Save the authentication state
            save_auth_state(context)
            
            print("\n✅ Autenticação salva com sucesso!")
            print("Próximas execuções usarão os cookies salvos.\n")
            
            return True
            
        except Exception as e:
            logger.error(f"Erro na primeira configuração: {e}")
            return False
        finally:
            browser.close()


def verificar_login(page: Page, timeout: int = 10000) -> bool:
    """
    Verifies if the user is logged in by checking for common elements.
    Waits up to 'timeout' milliseconds for any of the indicators to appear.
    
    Args:
        page: Playwright page
        timeout: Timeout in milliseconds
    
    Returns:
        bool: True if logged in
    """
    try:
        # Look for common logged-in indicators
        logged_in_selectors = [
            'button:has-text("Acessar")', # More specific locator for Acessar button
            'text="Acessar"',          # Fallback text
            '[data-testid="user-menu"]',
            '.user-avatar',
            'text=Dashboard',
            'text=Meus Aplicativos'    # Common header on Omie portal apps page
        ]
        
        logger.info(f"Verificando login (aguardando até {timeout/1000}s)...")
        
        # Poll for elements until timeout
        start_time = time.time()
        while (time.time() - start_time) * 1000 < timeout:
            for selector in logged_in_selectors:
                try:
                    # check if visible NOW
                    if page.locator(selector).first.is_visible():
                        logger.info(f"Login verificado com sucesso (encontrado: {selector})")
                        return True
                except:
                    continue
            
            page.wait_for_timeout(500) # Wait 500ms before next check
            
        logger.warning("Tempo esgotado na verificação de login.")
        return False
        
    except Exception as e:
        logger.warning(f"Não foi possível verificar login: {e}")
        return False


def realizar_login(page: Page) -> bool:
    """
    Realiza o login automático caso a sessão tenha expirado.
    Sequência:
    1. Preenche usuário (env OMIE_USER)
    2. Clica em "Continuar"
    3. Preenche senha (env OMIE_PASSWORD)
    4. Clica em "Entrar"
    """
    logger.info("Iniciando processo de login automático...")
    
    usuario = os.getenv("OMIE_USER")
    senha = os.getenv("OMIE_PASSWORD")
    
    if not usuario or not senha:
        logger.error("❌ Credenciais (OMIE_USER, OMIE_PASSWORD) não encontradas no .env")
        return False

    try:
        # Se não estiver na página de login, tenta ir (embora geralmente já esteja ou redirecione)
        if "login" not in page.url.lower() and "entrar" not in page.title().lower():
             logger.info("Redirecionando para página de login...")
             page.goto(OMIE_URL, timeout=30000)
        
        # 1. Coloca Usuário
        logger.info("Preenchendo e-mail...")

        # Seletor: getByRole('textbox', { name: 'Digite seu endereço de e-mail' })
        # (verificado ao vivo: é único; o id="email" é duplicado no DOM, não usar).
        email_input = page.get_by_role('textbox', name='Digite seu endereço de e-mail')
        email_input.wait_for(state="visible", timeout=30000)
        email_input.fill(usuario)

        # 2. Avança o passo de e-mail.
        # O clique em "Continuar" é frágil: após o fill o card sofre reflow e o
        # reCAPTCHA Enterprise pode sobrepor o botão -> Locator.click trava em
        # "visible/enabled/stable". E-mail e botão estão no mesmo <form
        # id="form-search-omie">, então submetemos via ENTER (verificado ao vivo
        # que avança o passo) e só caímos no clique/JS como fallback.
        logger.info("Avançando passo de e-mail (Enter)...")
        email_input.press("Enter")

        senha_input = page.get_by_role('textbox', name='Digite aqui sua senha')
        try:
            senha_input.wait_for(state="visible", timeout=10000)
        except Exception:
            logger.warning("Enter não avançou para a senha. Tentando clicar em 'Continuar'...")
            try:
                page.get_by_role('button', name='Continuar', exact=True).click(timeout=15000)
            except Exception:
                logger.warning("Clique em 'Continuar' falhou. Forçando via JavaScript...")
                page.evaluate("document.querySelector('#btn-continue')?.click()")

        # 3. Coloca Senha
        logger.info("Preenchendo senha...")
        # Seletor: getByRole('textbox', { name: 'Digite aqui sua senha' })
        senha_input.wait_for(state="visible", timeout=30000)
        senha_input.fill(senha)

        # 4. Submete o login (Enter, com fallback para o botão "Entrar")
        logger.info("Submetendo login...")
        senha_input.press("Enter")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        if not verificar_login(page, timeout=5000):
            logger.warning("Login não confirmado após Enter. Tentando botão 'Entrar'...")
            try:
                page.get_by_role('button', name='Entrar').click(timeout=15000)
            except Exception as e:
                logger.warning(f"Clique em 'Entrar' falhou: {e}")

        # Aguarda login
        logger.info("Aguardando confirmação de login...")
        page.wait_for_load_state("networkidle", timeout=30000)
        
        # Verifica se logou
        if verificar_login(page, timeout=10000):
            logger.info("✅ Login automático realizado com sucesso!")
            
            # Salva os novos cookies
            context = page.context
            save_auth_state(context)
            return True
        else:
            logger.error("❌ Falha na verificação pós-login.")
            return False

    except Exception as e:
        logger.error(f"❌ Erro durante login automático: {e}")
        return False

