"""
CNN 爬虫 - 通过 RSS 爬取 CNN Top Stories TOP 10
"""
from crawlers.base import RSSCrawler


class CNNCrawler(RSSCrawler):

    def __init__(self):
        super().__init__()
        self.name = "cnn"
        self.display_name = "CNN"
        self.language = "en"
        self.rss_url = "https://rss.cnn.com/rss/edition.rss"
        self.category = "Top Stories"
