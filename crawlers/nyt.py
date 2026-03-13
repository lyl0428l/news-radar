"""
New York Times 爬虫 - 通过 RSS 爬取 NYT 头条 TOP 10
"""
from crawlers.base import RSSCrawler


class NYTCrawler(RSSCrawler):

    detail_selectors = ["[name='articleBody']", ".meteredContent", ".StoryBodyCompanionColumn", "article"]

    def __init__(self):
        super().__init__()
        self.name = "nyt"
        self.display_name = "NYT"
        self.language = "en"
        self.rss_url = "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"
        self.category = "Top Stories"
