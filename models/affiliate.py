from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Text, TIMESTAMP, ForeignKey, CheckConstraint, Index

from models.base import Base


class Affiliate(Base):
    """Affiliate model"""
    __tablename__ = 'affiliates'
    
    affiliate_id = Column(BigInteger, primary_key=True)
    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'), primary_key=True)
    stars_earned = Column(Integer, default=0)
    status = Column(Text, default='pending')
    created_at = Column(TIMESTAMP, default=lambda: datetime.utcnow())
    paid_at = Column(TIMESTAMP, nullable=True)
    credit_balance = Column(Integer, default=0)
    
    __table_args__ = (
        CheckConstraint('credit_balance >= 0', name='check_non_negative_balance'),
        Index('idx_affiliates_shop_id', 'shop_id'),
        Index('idx_affiliates_status', 'status'),
        Index('idx_affiliates_affiliate_id', 'affiliate_id'),
        {'comment': 'Affiliates table'}
    )


class AffiliateCode(Base):
    """Affiliate code model"""
    __tablename__ = 'affiliate_codes'
    
    code = Column(String, primary_key=True)
    affiliate_id = Column(BigInteger)
    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'))
    created_at = Column(TIMESTAMP, default=lambda: datetime.utcnow())
    is_active = Column(Boolean, default=True)
    used_at = Column(TIMESTAMP, nullable=True)
    expires_at = Column(TIMESTAMP, nullable=True)
    
    __table_args__ = (
        Index('idx_affiliate_codes_affiliate_id', 'affiliate_id'),
        Index('idx_affiliate_codes_shop_id', 'shop_id'),
        Index('idx_affiliate_codes_is_active', 'is_active'),
        {'comment': 'Affiliate codes table'}
    )
