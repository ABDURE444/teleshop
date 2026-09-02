from sqlalchemy import BigInteger, Column, Index

from models.base import Base


class Message(Base):
    """Message model for tracking bot messages"""
    __tablename__ = 'messages'
    
    chat_id = Column(BigInteger, primary_key=True)
    message_id = Column(BigInteger, primary_key=True)
    
    __table_args__ = (
        Index('idx_messages_chat_id', 'chat_id'),
        {'comment': 'Messages table'}
    )
