from datetime import datetime

from sqlalchemy import Column, String, Text, TIMESTAMP, ForeignKey, Numeric, Index
from sqlalchemy.orm import relationship

from models.base import Base


class Payment(Base):
    """Payment model"""
    __tablename__ = 'payments'
    
    invoice_id = Column(String, primary_key=True)
    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'))
    plan = Column(Text)
    amount = Column(Numeric)
    currency = Column(Text, default='XTR')
    status = Column(Text)
    created_at = Column(TIMESTAMP, default=lambda: datetime.utcnow())
    updated_at = Column(TIMESTAMP)
    
    __table_args__ = (
        Index('idx_payments_shop_id', 'shop_id'),
        Index('idx_payments_invoice_id', 'invoice_id'),
        {'comment': 'Payments table'}
    )
    
    shop = relationship('Shop', back_populates='payments')
