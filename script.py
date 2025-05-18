import asyncio
import re
import shutil
import requests
from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from ebooklib import epub
from playwright.async_api import async_playwright

# === Settings ===
SECTIONS = {
    "Today's Articles": [
        "https://www.newyorker.com/latest",
    ]
}
ROOT_DIR = Path("the-new-yorker")
HEADERS = {"User-Agent": "Mozilla/5.0"}


def sanitize_filename(name):
    return re.sub(r'[^\w\-_\. ]', '_', name)


def create_epub(title, content, author, url, save_path, image_data=None):
    book = epub.EpubBook()
    book.set_identifier(url)
    book.set_title(title)
    book.set_language('en')
    book.add_author(author)

    chapter = epub.EpubHtml(title=title, file_name='chap.xhtml', lang='en')
    chapter.content = content
    book.add_item(chapter)

    if image_data:
        for img_url, img_filename in image_data:
            try:
                response = requests.get(img_url, stream=True, timeout=10)
                if response.status_code == 200:
                    img_item = epub.EpubItem(
                        uid=img_filename,
                        file_name=img_filename,
                        media_type=f'image/{img_filename.split(".")[-1]}',
                        content=response.content
                    )
                    book.add_item(img_item)
            except Exception as e:
                print(f"⚠️ Failed to fetch image: {img_url} - {e}")

    book.toc = (chapter,)
    book.spine = ['nav', chapter]
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())

    filename = sanitize_filename(title) + ".epub"
    epub.write_epub(str(save_path / filename), book)
    print(f"✅ Saved: {filename}")


def extract_clean_authors(author_tag):
    if not author_tag:
        return "The New Yorker"

    text = author_tag.get_text().strip()

    text = text.replace('\u00A0', ' ')

    prefixes = [
        r"Interview by",
        r"Photographs by",
        r"Reporting by",
        r"Words by",
        r"From",
        r"With",
        r"By",
        r"and"
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
    for _ in range(100):
        current_height = await page.evaluate("document.body.scrollHeight")
        if prev_height == current_height:
            break
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        prev_height = current_height


async def download_article(playwright, url, today_folder, week_folder):
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

        # Extract metadata
        title_tag = article_tag.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "Untitled"

        time_tag = article_tag.find("time")
        pub_date = datetime.today()
        if time_tag and time_tag.has_attr("datetime"):
            pub_date = datetime.fromisoformat(
                time_tag['datetime'].split("T")[0])

        author_tag = soup.find("span", class_=re.compile("byline"))
        author = extract_clean_authors(author_tag)
        print(author)

        # Replace images and collect them for embedding
        image_data = []
        for i, img in enumerate(article_tag.find_all("img"), start=1):
            if img.has_attr("src"):
                img_url = img["src"]
                img_ext = img_url.split(".")[-1].split("?")[0].split("#")[0]
                img_filename = f"image_{i}.{img_ext}"
                image_data.append((img_url, img_filename))
                img["src"] = img_filename

        body_html = str(article_tag)

        # Determine destination folder
        if pub_date.date() == datetime.today().date():
            create_epub(title, body_html, author,
                        url, today_folder, image_data)
        else:
            week_subfolder = week_folder / pub_date.strftime("%Y-%m-%d")
            week_subfolder.mkdir(parents=True, exist_ok=True)
            create_epub(title, body_html, author, url,
                        week_subfolder, image_data)

    except Exception as e:
        print(f"❌ Failed to save article: {url} - {e}")


def archive_old_articles(today_folder, week_folder):
    today = datetime.today()

    for epub_file in today_folder.glob("*.epub"):
        modified_time = datetime.fromtimestamp(epub_file.stat().st_mtime)
        if modified_time.date() < today.date():
            date_folder = week_folder / modified_time.strftime("%Y-%m-%d")
            date_folder.mkdir(parents=True, exist_ok=True)
            shutil.move(str(epub_file), date_folder / epub_file.name)
            print(f"📦 Moved old article to weekly archive: {epub_file.name}")

    for folder in week_folder.glob("*"):
        if folder.is_dir():
            try:
                folder_date = datetime.strptime(folder.name, "%Y-%m-%d")
                if today - folder_date > timedelta(days=7):
                    shutil.rmtree(folder)
                    print(f"🧹 Deleted old folder: {folder}")
            except ValueError:
                continue


async def main():
    today_folder = ROOT_DIR / "Today's Articles"
    week_folder = ROOT_DIR / "This Week"

    today_folder.mkdir(parents=True, exist_ok=True)
    week_folder.mkdir(parents=True, exist_ok=True)

    archive_old_articles(today_folder, week_folder)

    async with async_playwright() as playwright:
        links = await extract_article_links(playwright, SECTIONS["Today's Articles"])
        for link in links:
            await download_article(playwright, link, today_folder, week_folder)


if __name__ == "__main__":
    asyncio.run(main())
