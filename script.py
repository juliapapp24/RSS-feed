import asyncio
import re
import shutil
import requests
import subprocess
import json
from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from ebooklib import epub
from playwright.async_api import async_playwright

# === Settings ===
SECTIONS = {
    "Today's Articles": [
        "https://www.newyorker.com/latest", "https://www.newyorker.com/latest?page=2",  "https://www.newyorker.com/latest?page=3",  "https://www.newyorker.com/latest?page=4"
    ]
}

ROOT_DIR = Path("/Users/juliapappp/Calibre Library/the-new-yorker")
CALIBRE_LIBRARY_PATH = Path("/Users/juliapappp/Calibre Library")
DATA_FILE = ROOT_DIR / "article_data.json"
HEADERS = {"User-Agent": "Mozilla/5.0"}

ROOT_DIR.mkdir(parents=True, exist_ok=True)

today_articles = []
week_articles = []


def sanitize_filename(name):
    return re.sub(r'[^\w\-_\. ]', '_', name)


def import_to_calibre(epub_path):
    calibre_db = "/Applications/calibre.app/Contents/MacOS/calibredb"
    try:
        subprocess.run([
            calibre_db,
            "add",
            str(epub_path),
            "--with-library",
            str(CALIBRE_LIBRARY_PATH),
            "--automerge", "overwrite"
        ], check=True)
        print(f"📚 Added to Calibre: {epub_path.name}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to import {epub_path.name} into Calibre: {e}")


def create_combined_epub(today_articles, week_articles, save_path):
    book = epub.EpubBook()
    book.set_identifier(f"newyorker-digest-{datetime.today().isoformat()}")
    book.set_title("The New Yorker Digest")
    book.set_language('en')
    book.add_author("The New Yorker")

    spine = ['nav']
    toc = []

    def add_section(articles, section_title):
        section_items = []
        for i, article in enumerate(articles, 1):
            chap = epub.EpubHtml(
                title=article['title'], file_name=f'{section_title.lower().replace(" ", "_")}_{i}.xhtml', lang='en')
            chap.content = article['content']
            book.add_item(chap)
            spine.append(chap)
            section_items.append(chap)
        if section_items:
            toc.append((epub.Section(section_title), section_items))

    add_section(today_articles, "Today's News")
    add_section(week_articles, "This Week")

    book.toc = toc
    book.spine = spine

    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())

    filename = f"The New Yorker Digest - {datetime.today().strftime('%Y-%m-%d')}.epub"
    full_path = save_path / filename
    epub.write_epub(str(full_path), book)
    print(f"✅ Saved digest EPUB: {filename}")
    import_to_calibre(full_path)


def extract_clean_authors(author_tag):
    if not author_tag:
        return "The New Yorker"

    text = author_tag.get_text().strip()
    text = text.replace('\u00A0', ' ')

    prefixes = [
        r"Interview by", r"Photographs by", r"Reporting by", r"Words by",
        r"From", r"With", r"By", r"and"
    ]
    pattern = r"^(?:" + "|".join(prefixes) + r")\s+"
    cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return cleaned.strip()


async def extract_article_links(playwright, urls):
    browser = await playwright.chromium.launch()
    page = await browser.new_page()

    all_links = set()
    for url in urls:
        await page.goto(url)
        content = await page.content()
        soup = BeautifulSoup(content, 'html.parser')

        for a in soup.find_all("a", href=True):
            href = a['href']
            if (
                href.startswith("/") and
                len(href.split("/")) > 3 and
                any(href.startswith(p) for p in [
                    "/news/", "/culture/", "/magazine/", "/sports/", "/podcast/",
                    "/books/", "/newsletter/", "/humor/"
                ])
            ):
                full_url = "https://www.newyorker.com" + href
                all_links.add(full_url)

    await browser.close()
    return list(all_links)


async def scroll_to_bottom(page):
    prev_height = None
    for _ in range(200):
        current_height = await page.evaluate("document.body.scrollHeight")
        if prev_height == current_height:
            break
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        prev_height = current_height


async def download_article(playwright, url):

    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.goto(url)
    await scroll_to_bottom(page)

    content = await page.content()
    soup = BeautifulSoup(content, 'html.parser')
    await browser.close()

    try:
        article_tag = soup.find(
            "article", class_="article main-content", lang="en-US")
        if not article_tag:
            print(f"⚠️ Article tag not found: {url}")
            return

        title_tag = article_tag.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "Untitled"

        time_tag = article_tag.find("time")
        pub_date = datetime.today()
        if time_tag and time_tag.has_attr("datetime"):
            pub_date = datetime.fromisoformat(
                time_tag['datetime'].split("T")[0])

        author_tag = soup.find("span", class_=re.compile("byline"))
        author = extract_clean_authors(author_tag)

        image_data = []
        for i, img in enumerate(article_tag.find_all("img"), start=1):
            if img.has_attr("src"):
                img_url = img["src"]
                img_ext = img_url.split(".")[-1].split("?")[0].split("#")[0]
                img_filename = f"image_{i}.{img_ext}"
                image_data.append((img_url, img_filename))
                img["src"] = img_filename

        article_data = {
            "title": title,
            "author": author,
            "content": str(article_tag),
            "image_data": image_data,
            "url": url,
            "date": pub_date.date().isoformat()
        }

        if pub_date.date() == datetime.today().date():
            today_articles.append(article_data)
        else:
            week_articles.append(article_data)

    except Exception as e:
        print(f"❌ Failed to save article: {url} - {e}")


def load_articles():
    if DATA_FILE.exists():
        with open(DATA_FILE, 'r') as f:
            return json.load(f).get("articles", [])
    return []


def save_articles(articles):
    with open(DATA_FILE, 'w') as f:
        json.dump({"articles": articles}, f, indent=2)


def organize_articles(all_articles):
    today = datetime.today().date()
    yesterday = today - timedelta(days=1)
    week_limit = today - timedelta(days=7)

    updated_articles = []
    todays_articles = []
    weeks_articles = []

    for article in all_articles:
        art_date = datetime.fromisoformat(article["date"]).date()

        # Purge anything older than 7 days
        if art_date < week_limit:
            continue

        # Today
        if art_date == today:
            todays_articles.append(article)

        # This week includes today and last 6 days
        else:
            weeks_articles.append(article)

        updated_articles.append(article)

    return updated_articles, todays_articles, weeks_articles


async def main():
    all_articles = load_articles()
    async with async_playwright() as playwright:
        links = await extract_article_links(playwright, SECTIONS["Today's Articles"])
        existing_urls = {a["url"] for a in all_articles}
        for link in links:
            if link not in existing_urls:
                await download_article(playwright, link)

    new_urls = {a["url"] for a in today_articles + week_articles}
    all_articles += [a for a in today_articles +
                     week_articles if a["url"] not in {a["url"] for a in all_articles}]

    cleaned_articles, todays_articles_final, weeks_articles_final = organize_articles(
        all_articles)
    save_articles(cleaned_articles)
    create_combined_epub(todays_articles_final, weeks_articles_final, ROOT_DIR)


if __name__ == "__main__":
    asyncio.run(main())
