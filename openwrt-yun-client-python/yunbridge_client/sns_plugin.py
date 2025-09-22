"""
Amazon SNS Messaging Plugin for YunBridge Client
"""
from .plugin_base import MessagingPluginBase
import boto3

class SNSPlugin(MessagingPluginBase):
    def __init__(self, region, topic_arn, access_key, secret_key):
        self.region = region
        self.topic_arn = topic_arn
        self.access_key = access_key
        self.secret_key = secret_key
        self.client = None

    def connect(self):
        self.client = boto3.client(
            'sns',
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key
        )

    def publish(self, topic, message):
        if self.client is None:
            raise RuntimeError("SNS client is not connected. Call connect() before publish().")
        self.client.publish(
            TopicArn=topic or self.topic_arn,
            Message=message
        )

    def subscribe(self, topic, callback):
        # SNS does not support direct subscribe/receive in client; use SQS or Lambda integration
        raise NotImplementedError("SNS subscribe not supported in client. Use SQS or Lambda.")

    def disconnect(self):
        pass  # No persistent connection to close
