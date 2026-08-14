"""Quick script to scrape a Kindle book via the plugin directly."""

import asyncio
import sys


async def main():
    asin = sys.argv[1] if len(sys.argv) > 1 else "B003NX6Z3W"

    # Import the plugin's tool handler directly
    sys.path.insert(0, str(__import__("pathlib").Path.home() / ".research-engine/plugins/kindle@0.1.0"))
    from code.tools.scrape_kindle_book import tool_handler

    print(f"Scraping book ASIN: {asin}")
    print("A browser window will open. Log in to Amazon if prompted.\n")

    result = await tool_handler(
        None,  # container — not needed for this plugin
        book_asin=asin,
    )

    if result["status"] == "success":
        print(f"Title:      {result['title']}")
        print(f"Pages:      {result['page_count']}")
        print(f"Text size:  {result['text_length']} chars")
        print(f"Saved to:   {result['output_path']}")
        print(f"\nPreview:\n{result['text_preview']}")
    else:
        print(f"Error: {result.get('message', result)}")


if __name__ == "__main__":
    asyncio.run(main())
