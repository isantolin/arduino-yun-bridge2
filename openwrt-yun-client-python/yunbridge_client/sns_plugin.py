"""
Amazon SNS Messaging Plugin for YunBridge Client
Improvements: rotating logging, configuration validation, robust error handling.
"""
from .plugin_base import MessagingPluginBase
import logging
from logging.handlers import RotatingFileHandler

LOG_PATH = '/tmp/yunbridge_sns_plugin.log'
handler = RotatingFileHandler(LOG_PATH, maxBytes=1000000, backupCount=3)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
handler.setFormatter(formatter)
logger = logging.getLogger("yunbridge.sns_plugin")
logger.setLevel(logging.INFO)  # Change to DEBUG for more detail
if not logger.hasHandlers():
    logger.addHandler(handler)

class SNSPlugin(MessagingPluginBase):
    def __init__(self, region, topic_arn, access_key, secret_key):
        if not (region and topic_arn and access_key and secret_key):
            raise ValueError("All SNS config params are required")
        self.region = region
        self.topic_arn = topic_arn
        self.access_key = access_key
        self.secret_key = secret_key
        self.client = None

    def connect(self):
        try:
            import boto3
            self.client = boto3.client(
                'sns',
                region_name=self.region,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key
            )
            logger.info(f"Connected to SNS region {self.region}")
        except Exception as e:
            logger.error(f"SNS connect error: {e}")
            raise

    def publish(self, topic, message):
        if not (topic or self.topic_arn) or message is None:
            logger.error("SNS publish: topic/arn and message required")
            raise ValueError("SNS publish: topic/arn and message required")
        if self.client is None:
            raise RuntimeError("SNS client is not connected. Call connect() before publish().")
        try:
            self.client.publish(
                TopicArn=topic or self.topic_arn,
                Message=message
            )
            logger.debug(f"Published to SNS {topic or self.topic_arn}: {message}")
        except Exception as e:
            logger.error(f"SNS publish error: {e}")
            raise

    def subscribe(self, topic, callback):
        if not topic or not callable(callback):
            logger.error("SNS subscribe: valid topic and callback required")
            raise ValueError("SNS subscribe: valid topic and callback required")
        # SNS does not support direct subscribe/receive in client; use SQS or Lambda integration
        logger.warning("SNS subscribe not supported in client. Use SQS or Lambda.")
        raise NotImplementedError("SNS subscribe not supported in client. Use SQS or Lambda.")

    def disconnect(self):
        logger.info("SNS disconnect (noop)")
