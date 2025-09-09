from flask import Flask, request, jsonify, Response
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup, NavigableString, Tag
import time
import json
import re
import threading
from urllib.parse import urljoin, urlparse, quote_plus
import logging
import atexit

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Patterns for meaningful text detection
email_pattern = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+')
phone_pattern = re.compile(r'(\+?\d{1,3}[-.\s]?)?(\(?\d{3}\)?[-.\s]?){1,2}\d{4}')
url_pattern = re.compile(r'https?://[^\s]+')

def is_meaningful_text(text):
    if email_pattern.search(text):
        return True
    if phone_pattern.search(text):
        return True
    if url_pattern.search(text):
        return True
    # Also consider non-empty text meaningful
    if text.strip():
        return True
    return False

def clean_tag(tag):
    # If it's a NavigableString, return stripped text
    if isinstance(tag, NavigableString):
        text = tag.strip()
        if text:
            return text
        else:
            return None

    # If it's an <a> tag with mailto or http(s), keep it as is
    if tag.name == 'a':
        href = tag.get('href', '')
        text = tag.get_text(strip=True)
        if href.startswith('mailto:') or href.startswith('http'):
            # Return a new <a> tag with href and text
            new_tag = Tag(name='a')
            new_tag['href'] = href
            new_tag.string = text
            return new_tag
        else:
            # Otherwise, just return the text content
            return text if text else None

    # For other tags, recursively clean children
    cleaned_children = []
    for child in tag.children:
        cleaned_child = clean_tag(child)
        if cleaned_child:
            cleaned_children.append(cleaned_child)

    # Flatten strings and tags into a list of strings or tags
    # Join consecutive strings with spaces
    result = []
    buffer = []
    def flush_buffer():
        if buffer:
            result.append(' '.join(buffer))
            buffer.clear()

    for item in cleaned_children:
        if isinstance(item, str):
            buffer.append(item)
        else:
            flush_buffer()
            result.append(item)
    flush_buffer()

    # If result is empty, return None
    if not result:
        return None

    # If only one item and it's string, return it directly
    if len(result) == 1 and isinstance(result[0], str):
        return result[0]

    # Otherwise, wrap all items in a <section> tag (valid HTML)
    section = Tag(name='section')
    for item in result:
        if isinstance(item, str):
            # Add text with a space separator
            if section.contents:
                section.append(' ')
            section.append(item)
        else:
            section.append(item)
    return section

class SingleBrowserManager:
    def __init__(self):
        self.driver = None
        self.lock = threading.Lock()  # Thread safety for shared browser
        self.setup_driver()
    
    def setup_driver(self):
        """Initialize Chrome WebDriver with appropriate options"""
        chrome_options = Options()
        
        # Add options for better performance and stability
        chrome_options.add_argument('--headless=new')  # Chrome 109+ headless mode
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')

        # Explicit paths for your VPS
        chrome_options.binary_location = "/usr/bin/google-chrome"
        service = Service("/usr/bin/chromedriver")

        try:
            self.driver = webdriver.Chrome(service=service, options=chrome_options)
            logger.info("Chrome WebDriver initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            return False

    
    def restart_driver(self):
        """Restart the WebDriver if needed"""
        with self.lock:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
            return self.setup_driver()
    
    def is_healthy(self):
        """Check if the driver is healthy"""
        try:
            if self.driver is None:
                return False
            # Try to get current URL to test responsiveness
            current_url = self.driver.current_url
            return True
        except:
            return False
    
    def get_driver(self):
        """Get the driver instance with health check"""
        if not self.is_healthy():
            logger.warning("Driver is unhealthy, attempting restart...")
            self.restart_driver()
        return self.driver
    
    def close(self):
        """Close the WebDriver"""
        with self.lock:
            if self.driver:
                try:
                    self.driver.quit()
                    logger.info("WebDriver closed successfully")
                except Exception as e:
                    logger.error(f"Error closing WebDriver: {e}")

class DuckDuckGoScraper:
    def __init__(self, browser_manager):
        self.browser_manager = browser_manager
    
    def extract_base_url(self, url):
        """Extract base URL from a full URL"""
        try:
            parsed = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}"
        except:
            return ""
    
    def extract_results_from_page(self, page_num):
        """Extract search results from current page"""
        results = []
        driver = self.browser_manager.get_driver()
        
        # Wait for results to load with longer timeout
        wait = WebDriverWait(driver, 15)
        
        # Wait for search results container with DuckDuckGo specific selectors
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="result"], .web-result, .result')))
        except TimeoutException:
            print(f"Timeout waiting for results on page {page_num}")
            return []
        
        # DuckDuckGo specific selectors for search results
        result_selectors = [
            '[data-testid="result"]',  # Modern DuckDuckGo
            '.web-result',             # Alternative DuckDuckGo
            '.result',                 # Fallback
            'article[data-testid="result"]',
            '.result__body'
        ]
        
        result_elements = []
        for selector in result_selectors:
            try:
                result_elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if result_elements:
                    print(f"Found {len(result_elements)} elements with selector: {selector}")
                    break
            except NoSuchElementException:
                continue
        
        if not result_elements:
            print(f"No search results found on page {page_num}")
            return []
        
        for i, result in enumerate(result_elements):
            try:
                # Extract title with DuckDuckGo specific selectors
                title_selectors = [
                    'h2 a[data-testid="result-title-a"]',  # Modern DuckDuckGo
                    'h2 a',
                    'h3 a',
                    'a[data-testid="result-title-a"]',
                    '.result__title a',
                    '.result-title a',
                    '.result__a'
                ]
                
                title = ""
                title_element = None
                
                for selector in title_selectors:
                    try:
                        title_element = result.find_element(By.CSS_SELECTOR, selector)
                        title = title_element.text.strip()
                        if title:
                            break
                    except NoSuchElementException:
                        continue
                
                # Skip if no title found
                if not title:
                    continue
                
                # Extract URL
                url = ""
                base_url = ""
                if title_element:
                    try:
                        url = title_element.get_attribute('href')
                        if url:
                            base_url = self.extract_base_url(url)
                    except:
                        pass
                
                # Skip if no URL found
                if not url:
                    continue
                
                # Extract snippet/description with DuckDuckGo specific selectors
                snippet_selectors = [
                    '[data-result="snippet"]',
                    '.result__snippet',
                    '.result-snippet',
                    'div[data-testid="result-snippet"]',
                    '.VwiC3b',
                    '.result__body',
                    'span[data-testid="result-snippet"]'
                ]
                
                snippet = ""
                for selector in snippet_selectors:
                    try:
                        snippet_element = result.find_element(By.CSS_SELECTOR, selector)
                        snippet = snippet_element.text.strip()
                        if snippet:
                            break
                    except NoSuchElementException:
                        continue
                
                # If no snippet found with specific selectors, try to extract from result text
                if not snippet:
                    try:
                        full_text = result.text.strip()
                        # Remove title from full text to get snippet
                        if title in full_text:
                            snippet = full_text.replace(title, "").strip()
                        else:
                            snippet = full_text
                        
                        # Clean up snippet (remove URL if present)
                        if url in snippet:
                            snippet = snippet.replace(url, "").strip()
                            
                        # Limit snippet length
                        if len(snippet) > 300:
                            snippet = snippet[:300] + "..."
                            
                    except:
                        snippet = "No description available"
                
                # Only add result if we have both title and URL
                if title and url:
                    result_data = {
                        "position": len(results) + 1,
                        "page": page_num,
                        "title": title,
                        "url": url,
                        "base_url": base_url,
                        "snippet": snippet
                    }
                    results.append(result_data)
                    print(f"Extracted result {len(results)}: {title[:50]}...")
            
            except Exception as e:
                print(f"Error extracting result {i} on page {page_num}: {str(e)}")
                continue
        
        return results
    
    def navigate_to_next_page(self):
        """Navigate to the next page of results"""
        driver = self.browser_manager.get_driver()
        
        try:
            # First, try the exact button ID we found
            try:
                more_button = driver.find_element(By.ID, "more-results")
                if more_button and more_button.is_displayed() and more_button.is_enabled():
                    print("Found 'more-results' button by ID, clicking...")
                    driver.execute_script("arguments[0].click();", more_button)
                    time.sleep(4)  # Wait for new results to load
                    return True
            except (NoSuchElementException, Exception) as e:
                print(f"ID selector failed: {str(e)}")
            
            # Try CSS selectors for the button
            css_selectors = [
                'button#more-results',
                'button[id="more-results"]',
                'div.rdxznaZygY2CryNa5yzk button',
                '.rdxznaZygY2CryNa5yzk button',
                'button.wE5p3MOcL8UVdJhgH3V1'
            ]
            
            for selector in css_selectors:
                try:
                    load_more_button = driver.find_element(By.CSS_SELECTOR, selector)
                    if load_more_button and load_more_button.is_displayed() and load_more_button.is_enabled():
                        print(f"Found button with selector: {selector}, clicking...")
                        driver.execute_script("arguments[0].click();", load_more_button)
                        time.sleep(4)
                        return True
                except (NoSuchElementException, Exception) as e:
                    print(f"CSS selector {selector} failed: {str(e)}")
                    continue
            
            # Try XPath selectors for multilingual support
            xpath_selectors = [
                "//button[@id='more-results']",
                "//button[contains(text(), 'More results')]",
                "//button[contains(text(), 'Davantage de résultats')]",
                "//button[contains(text(), 'Más resultados')]",
                "//button[contains(text(), 'Mehr Ergebnisse')]",
                "//button[contains(text(), 'Più risultati')]",
                "//button[contains(text(), 'Load more')]",
                "//button[contains(text(), 'Show more')]",
                "//div[contains(@class, 'rdxznaZygY2CryNa5yzk')]//button"
            ]
            
            for xpath in xpath_selectors:
                try:
                    element = driver.find_element(By.XPATH, xpath)
                    if element and element.is_displayed() and element.is_enabled():
                        print(f"Found button with XPath: {xpath}, clicking...")
                        driver.execute_script("arguments[0].click();", element)
                        time.sleep(4)
                        return True
                except (NoSuchElementException, Exception) as e:
                    print(f"XPath {xpath} failed: {str(e)}")
                    continue
            
            # Try to find any button that might be the "more results" button
            try:
                all_buttons = driver.find_elements(By.TAG_NAME, "button")
                for button in all_buttons:
                    button_text = button.text.lower()
                    button_id = button.get_attribute('id') or ''
                    
                    # Check if this looks like a "more results" button
                    if any(keyword in button_text for keyword in ['more', 'davantage', 'más', 'mehr', 'più', 'load', 'show']) or \
                       'more-results' in button_id:
                        if button.is_displayed() and button.is_enabled():
                            print(f"Found potential more button: text='{button_text}', id='{button_id}', clicking...")
                            driver.execute_script("arguments[0].click();", button)
                            time.sleep(4)
                            return True
            except Exception as e:
                print(f"Button search failed: {str(e)}")
            
            # Try scrolling approach as backup
            try:
                print("Trying scroll approach...")
                initial_height = driver.execute_script("return document.body.scrollHeight")
                
                # Scroll to bottom
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(3)
                
                # Check if page height increased (indicating new content loaded)
                new_height = driver.execute_script("return document.body.scrollHeight")
                
                if new_height > initial_height:
                    print("New content loaded via scrolling")
                    return True
            except Exception as e:
                print(f"Scroll approach failed: {str(e)}")
            
            # URL manipulation as last resort
            try:
                print("Trying URL manipulation...")
                current_url = driver.current_url
                
                # DuckDuckGo uses 's=' parameter for pagination offset
                if 's=' in current_url:
                    import re
                    match = re.search(r's=(\d+)', current_url)
                    if match:
                        current_start = int(match.group(1))
                        new_start = current_start + 30
                        new_url = re.sub(r's=\d+', f's={new_start}', current_url)
                        print(f"Navigating to: {new_url}")
                        driver.get(new_url)
                        time.sleep(4)
                        return True
                else:
                    separator = '&' if '?' in current_url else '?'
                    new_url = f"{current_url}{separator}s=30"
                    print(f"Navigating to: {new_url}")
                    driver.get(new_url)
                    time.sleep(4)
                    return True
            except Exception as e:
                print(f"URL manipulation failed: {str(e)}")
            
            print("All pagination methods failed")
            return False
            
        except Exception as e:
            print(f"Error navigating to next page: {str(e)}")
            return False
    
    def search_duckduckgo(self, query, max_pages=3):
        """Search DuckDuckGo and extract results from multiple pages"""
        driver = self.browser_manager.get_driver()
        
        if not driver:
            return {"error": "Failed to get browser driver"}
        
        try:
            # Use thread lock to ensure thread safety
            with self.browser_manager.lock:
                # Navigate to DuckDuckGo
                clean_query = query.replace('\"','')
                search_url = f"https://duckduckgo.com/?q={quote_plus(clean_query)}"
                driver.get(search_url)
                
                all_results = []
                current_page = 1
                
                while current_page <= max_pages:
                    print(f"Scraping page {current_page}...")
                    
                    # Extract results from current page
                    page_results = self.extract_results_from_page(current_page)
                    
                    if not page_results:
                        print(f"No results found on page {current_page}")
                        break
                    
                    # Update position numbers to be continuous across pages
                    for result in page_results:
                        result["position"] = len(all_results) + 1
                        result["query"] = query
                        all_results.append(result)
                    
                    print(f"Found {len(page_results)} results on page {current_page}")
                    
                    # Try to navigate to next page if not on last requested page
                    if current_page < max_pages:
                        if not self.navigate_to_next_page():
                            print(f"Could not navigate to page {current_page + 1}, stopping pagination")
                            break
                        
                        # Wait a bit between page requests to be respectful
                        time.sleep(1)
                    
                    current_page += 1
                
                return {
                    "query": query,
                    "pages_scraped": current_page - 1,
                    "total_results": len(all_results),
                    "base_search_url": search_url,
                    "results": all_results
                }
            
        except Exception as e:
            return {"error": f"Search failed: {str(e)}", "query": query}

# Initialize global instances
browser_manager = SingleBrowserManager()
search_scraper = DuckDuckGoScraper(browser_manager)

@app.route('/search', methods=['GET', 'POST'])
def search_duckduckgo():
    """
    Search DuckDuckGo and return results from multiple pages using enhanced method
    """
    try:
        # Get query and max_pages from request
        if request.method == 'POST':
            data = request.get_json()
            if not data or 'query' not in data:
                return jsonify({"error": "Missing 'query' parameter in JSON body"}), 400
            query = data['query']
            max_pages = data.get('max_pages', 3)  # Default to 3 pages
        else:  # GET request
            query = request.args.get('query')
            if not query:
                return jsonify({"error": "Missing 'query' parameter"}), 400
            max_pages = int(request.args.get('max_pages', 3))  # Default to 3 pages
        
        # Validate query
        if not query.strip():
            return jsonify({"error": "Query cannot be empty"}), 400
        
        # Validate max_pages
        if max_pages < 1:
            max_pages = 1
        elif max_pages > 10:  # Limit to prevent abuse
            max_pages = 10
        
        logger.info(f"Starting DuckDuckGo search for: {query}, pages: {max_pages}")
        
        # Perform search using enhanced method
        results = search_scraper.search_duckduckgo(query.strip(), max_pages)
        
        # Return results
        if "error" in results:
            return jsonify(results), 500
        else:
            logger.info(f"Search completed successfully. Total results: {results.get('total_results', 0)}")
            return jsonify(results), 200
            
    except Exception as e:
        logger.error(f"Search error: {e}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

@app.route('/scrape', methods=['GET'])
def scrape_url():
    """
    Scrape a specific URL and return cleaned HTML content using the shared browser
    """
    try:
        url = request.args.get('url')
        
        if not url:
            return jsonify({'error': 'URL parameter is required'}), 400
        
        # Validate URL
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return jsonify({'error': 'Invalid URL format'}), 400
        
        logger.info(f"Scraping URL: {url}")
        
        driver = browser_manager.get_driver()
        if not driver:
            return jsonify({'error': 'Browser driver is not available'}), 500
        
        try:
            # Use thread lock to ensure thread safety
            with browser_manager.lock:
                driver.get(url)
                # Wait for page to load
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                # Additional wait for dynamic content
                time.sleep(2)
                html = driver.page_source
        except Exception as e:
            return jsonify({'error': f'Failed to load URL: {str(e)}'}), 400

        soup = BeautifulSoup(html, 'html.parser')

        # Remove <script> and <style> tags
        for tag in soup(['script', 'style']):
            tag.decompose()

        body = soup.body or soup
        cleaned = clean_tag(body)

        # If cleaned is None, return empty string
        output_html = str(cleaned) if cleaned else ''

        logger.info(f"Successfully scraped and cleaned {url} - Content length: {len(output_html)}")

        # Return raw HTML with UTF-8 encoding and no escaping
        return Response(output_html, content_type='text/html; charset=utf-8')
        
    except Exception as e:
        logger.error(f"Scraping error for {url}: {e}")
        return jsonify({
            'error': f'Scraping failed: {str(e)}',
            'url': url,
            'success': False
        }), 500

@app.route('/status', methods=['GET'])
def status():
    """Check if the browser is running and healthy"""
    try:
        driver = browser_manager.get_driver()
        if driver and browser_manager.is_healthy():
            current_url = driver.current_url
            return jsonify({
                'status': 'healthy',
                'browser_active': True,
                'current_url': current_url
            })
        else:
            return jsonify({
                'status': 'unhealthy',
                'browser_active': False,
                'message': 'Browser driver is not responsive'
            }), 500
    except Exception as e:
        logger.warning(f"Browser not responsive: {e}")
        try:
            # Try to restart the browser
            browser_manager.restart_driver()
            return jsonify({
                'status': 'restarted',
                'browser_active': True,
                'message': 'Browser was restarted'
            })
        except Exception as restart_error:
            logger.error(f"Failed to restart browser: {restart_error}")
            return jsonify({
                'status': 'error',
                'browser_active': False,
                'error': str(restart_error)
            }), 500

@app.route('/restart', methods=['POST'])
def restart_browser():
    """Restart the browser instance"""
    try:
        browser_manager.restart_driver()
        logger.info("Browser restarted successfully")
        return jsonify({'message': 'Browser restarted successfully'})
    except Exception as e:
        logger.error(f"Failed to restart browser: {e}")
        return jsonify({'error': f'Failed to restart browser: {str(e)}'}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "message": "Combined Selenium API is running"}), 200

@app.route('/', methods=['GET'])
def home():
    """Home endpoint with API documentation"""
    doc = {
        "name": "Flask Selenium Scraper API",
        "version": "4.0.0",
        "description": "API for DuckDuckGo search and web scraping using a single shared Selenium browser instance",
        "endpoints": {
            "/search": {
                "methods": ["GET", "POST"],
                "description": "Search DuckDuckGo with enhanced pagination and result extraction",
                "parameters": {
                    "query": "Search query string (supports quotes and special characters)",
                    "max_pages": "Maximum number of pages to scrape (default: 3, max: 10)"
                },
                "examples": {
                    "GET": "/search?query=\"machine learning\"&max_pages=5",
                    "POST": {
                        "url": "/search",
                        "body": {
                            "query": "\"artificial intelligence\" tutorial",
                            "max_pages": 3
                        }
                    }
                }
            },
            "/scrape": {
                "methods": ["GET"],
                "description": "Scrape a URL and return cleaned HTML content (removes scripts, styles, and cleans structure)",
                "parameters": {
                    "url": "URL to scrape"
                },
                "example": "/scrape?url=https://example.com"
            },
            "/status": {
                "methods": ["GET"],
                "description": "Check browser health and status"
            },
            "/restart": {
                "methods": ["POST"],
                "description": "Restart the browser instance"
            },
            "/health": {
                "methods": ["GET"],
                "description": "API health check endpoint"
            }
        },
        "features": [
            "Single shared browser instance for all operations",
            "Thread-safe browser access with locking",
            "Enhanced DuckDuckGo search with multiple pagination methods",
            "Cleaned HTML scraping with meaningful content extraction",
            "Automatic browser health checks and restart capabilities",
            "Comprehensive error handling and logging"
        ],
        "architecture": "Uses a single Chrome browser instance shared across all endpoints with thread-safe locking"
    }
    return jsonify(doc), 200

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception: {e}")
    return jsonify({'error': 'Internal server error'}), 500

# Cleanup on app shutdown
def cleanup():
    logger.info("Shutting down browser manager...")
    browser_manager.close()

atexit.register(cleanup)

if __name__ == '__main__':
    logger.info("Starting Flask Selenium Scraper API with Single Browser Instance")
    logger.info("Browser instance initialized and ready")
    logger.info("Available endpoints:")
    logger.info("- GET/POST /search")
    logger.info("- GET /scrape")
    logger.info("- GET /status")
    logger.info("- POST /restart")
    logger.info("- GET /health")
    
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)