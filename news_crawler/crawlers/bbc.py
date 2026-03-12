"""
BBC News 爬虫 - 通过 RSS 爬取 BBC Top Stories TOP 10
"""
from crawlers.base import RSSCrawler


class BBCCrawler(RSSCrawler):

    def __init__(self):
        super().__init__()
        self.name = "bbc"
        self.display_name = "BBC News"
        self.language = "en"
        self.rss_url = "https://feeds.bbci.co.uk/news/rss.xml"
        self.category = "Top Stories"
