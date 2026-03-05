#!/usr/bin/env python3

"""
NYC Sublet Listings Scraper

This script extracts sublet listings from Listings Project's NYC page.
"""

import csv
import re
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# Configuration
URL = "https://www.listingsproject.com/real-estate/new-york-city/sublets"
OUTPUT_FILE = "/tmp/nyc_sublets.csv"


def init_browser():
    """Initialize and return browser instance"""
    # Set up Chrome options
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")

    # Initialize driver
    driver = webdriver.Chrome(options=chrome_options)

    # Navigate to the main page
    print("Navigating to Listings Project NYC sublets page...")
    driver.get(URL)

    # Wait for the page to load
    print("Waiting for page to load...")
    time.sleep(10)  # Simple wait to ensure page loads

    # Take a screenshot for verification
    driver.save_screenshot("/tmp/listings_page.png")
    print("Page loaded and screenshot saved.")

    return driver


def get_pagination_links(driver):
    """Find all pagination links and return a list of URLs to all pages"""
    print("Looking for pagination links...")

    pagination_urls = []
    current_url = driver.current_url
    pagination_urls.append(current_url)  # Start with current page

    try:
        # Look for page number elements - we need page links 1-21
        page_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'page=')]")
        highest_page = 1

        for link in page_links:
            href = link.get_attribute("href")
            # Make sure this is actually a page link, not a listing
            if href and "listings/" not in href:
                # Extract the page number
                page_match = re.search(r"page=(\d+)", href)
                if page_match:
                    page_num = int(page_match.group(1))
                    highest_page = max(highest_page, page_num)

                if href not in pagination_urls:
                    pagination_urls.append(href)

        # Now construct the missing pages
        base_url = current_url
        if "?" in base_url:
            base_url += "&"
        else:
            base_url += "?"

        base_url = base_url.split("page=")[0]

        all_pagination_urls = [current_url]  # Start with page 1

        # Add pages 2 through highest_page
        for page in range(2, highest_page + 1):
            page_url = f"{base_url}page={page}"
            all_pagination_urls.append(page_url)

        print(
            f"Found {len(all_pagination_urls)} pagination URLs (pages 1-{highest_page})"
        )
        return all_pagination_urls

    except Exception as e:
        print(f"Error finding pagination links: {e}")
        # Return at least the current page URL
        return [driver.current_url]


def extract_listings_from_main_page(driver):
    """Extract all listings from the main page (without visiting individual pages)"""
    print("Extracting listings from current page...")

    # Based on the screenshot, each listing is a container that has both image and details
    # Let's find the actual listing blocks only (not the header elements that got included)
    listing_sections = []

    try:
        # Find listing blocks by key features we see in the screenshot
        # Each listing has an image and details in a flex container
        listing_blocks = driver.find_elements(
            By.CSS_SELECTOR, "div.flex.flex-col.md\\:flex-row.mb-6"
        )
        if not listing_blocks:
            listing_blocks = driver.find_elements(
                By.XPATH, "//div[contains(@class, 'flex') and contains(@class, 'mb-6')]"
            )

        # Filter out any listings that don't have an image or title/description
        real_listings = []
        for block in listing_blocks:
            # Skip blocks that don't have images or links to listings
            try:
                link_text = block.find_element(By.TAG_NAME, "a").get_attribute("href")
                if "/listings/" in link_text:
                    real_listings.append(block)
            except:
                continue

        listing_sections = real_listings
        print(f"Found {len(listing_sections)} listing blocks")
    except Exception as e:
        print(f"Error finding listing sections: {e}")

    results = []
    for i, section in enumerate(listing_sections):
        try:
            # Extract data from each listing section
            listing_data = {
                "title": "Not found",
                "price": "Not found",
                "start_date": "Not found",
                "end_date": "Not found",
                "location": "Not found",
                "description": "Not found",
                "link": "Not found",
                "image_url": "Not found",
            }

            # Get the link and image URL
            try:
                # First, get the listing URL from any anchor that points to a listing
                link_elems = section.find_elements(
                    By.CSS_SELECTOR, "a[href*='/listings/']"
                )
                if link_elems:
                    listing_data["link"] = link_elems[0].get_attribute("href")

                    # Try to get the image URL from inside the link
                    try:
                        img_elem = link_elems[0].find_element(By.TAG_NAME, "img")
                        listing_data["image_url"] = img_elem.get_attribute("src")
                    except:
                        # Try searching for the image elsewhere in the container
                        try:
                            img_elem = section.find_element(By.TAG_NAME, "img")
                            listing_data["image_url"] = img_elem.get_attribute("src")
                        except:
                            pass
            except Exception as e:
                print(f"Error getting link/image: {e}")

            # Get the title - it's in a teal colored element
            try:
                # Based on the screenshot, the title is a teal-colored heading
                title_elems = section.find_elements(
                    By.CSS_SELECTOR, "h3.text-teal, h2.text-teal, [class*='text-teal']"
                )
                if title_elems:
                    listing_data["title"] = title_elems[0].text.strip()
                else:
                    # If no teal class, try to find any h2 or h3
                    title_elems = section.find_elements(By.CSS_SELECTOR, "h2, h3")
                    if title_elems:
                        listing_data["title"] = title_elems[0].text.strip()
            except Exception as e:
                print(f"Error getting title: {e}")

            # Get the price and dates - they're in teal background elements
            try:
                teal_bg_elems = section.find_elements(
                    By.CSS_SELECTOR, "[class*='bg-teal']"
                )
                if teal_bg_elems:
                    # First element is typically price
                    if len(teal_bg_elems) >= 1:
                        listing_data["price"] = teal_bg_elems[0].text.strip()

                    # Second element is typically dates
                    if len(teal_bg_elems) >= 2:
                        date_text = teal_bg_elems[1].text.strip()
                        # Parse date range (e.g., "June 22, 2025 - August 31, 2025")
                        date_parts = date_text.split("-")
                        if len(date_parts) == 2:
                            listing_data["start_date"] = date_parts[0].strip()
                            listing_data["end_date"] = date_parts[1].strip()
                        else:
                            # If not in expected format, store the whole text
                            listing_data["start_date"] = date_text
            except Exception as e:
                print(f"Error getting price/dates: {e}")

            # Get the location
            try:
                # Location is above the title
                location_elems = section.find_elements(
                    By.XPATH,
                    ".//div[contains(text(), 'Brooklyn') or contains(text(), 'Manhattan') or contains(text(), 'Queens')]",
                )
                if location_elems:
                    listing_data["location"] = location_elems[0].text.strip()
            except Exception as e:
                print(f"Error getting location: {e}")

            # Get the description - typically a paragraph element
            try:
                desc_elems = section.find_elements(By.TAG_NAME, "p")
                if desc_elems:
                    # Join multiple paragraph texts if needed
                    description = "\n".join([elem.text.strip() for elem in desc_elems])
                    listing_data["description"] = description
            except Exception as e:
                print(f"Error getting description: {e}")

            # Add to results only if we found something meaningful
            if listing_data["link"] != "Not found" and any(
                v != "Not found" for v in listing_data.values()
            ):
                results.append(listing_data)

            # Print the first listing for debugging if we're on the first page
            if i == 0 and len(results) == 1:
                print(f"\nSample listing:\n{listing_data}")

        except Exception as e:
            print(f"Error processing listing section: {e}")

    return results


def save_to_csv(listings, filename):
    """Save listings to a CSV file"""
    with open(filename, "w", newline="", encoding="utf-8") as csvfile:
        # Define the field names based on our listing structure
        fieldnames = [
            "title",
            "price",
            "start_date",
            "end_date",
            "location",
            "description",
            "link",
            "image_url",
        ]

        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter="|")
        writer.writeheader()

        for listing in listings:
            writer.writerow(listing)

    print(f"Saved {len(listings)} listings to {filename}")


def main():
    """Main function to run the scraper"""
    driver = init_browser()
    try:
        # Find all pagination pages
        page_urls = get_pagination_links(driver)
        print(f"Found {len(page_urls)} pages to scrape")

        # Extract listings from each page
        all_listings = []

        # Track URLs we've already visited to avoid duplicates
        visited_urls = set()

        for i, page_url in enumerate(page_urls):
            # Skip if we've already visited this URL
            if page_url in visited_urls:
                continue

            visited_urls.add(page_url)
            print(f"\nScraping page {i+1}/{len(page_urls)}: {page_url}")

            # Navigate to the page
            driver.get(page_url)
            time.sleep(5)  # Wait for page to load

            # Extract listings from this page
            page_listings = extract_listings_from_main_page(driver)
            all_listings.extend(page_listings)

            print(f"Found {len(page_listings)} listings on this page")

        # Print results
        print(f"\nTotal listings found across all pages: {len(all_listings)}")

        # Save all listings to CSV
        save_to_csv(all_listings, OUTPUT_FILE)

        # Expected count is 206 listings
        if len(all_listings) == 206:
            print("SUCCESS: Found the expected 206 listings!")
        else:
            print(f"WARNING: Found {len(all_listings)} listings, but expected 206.")

    finally:
        driver.quit()
        print("Browser closed.")


if __name__ == "__main__":
    main()
