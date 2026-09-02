from datetime import datetime
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Text, TIMESTAMP, ForeignKey, CheckConstraint, Index
from sqlalchemy.sql import text as sql_text

from models.base import Base, BigIntPK


class AffiliatePayment(Base):
    """Affiliate payment model for tracking payments processed by Shop Bots"""
    __tablename__ = 'affiliate_payments'
    
    invoice_id = Column(String, primary_key=True)
    shop_id = Column(String, ForeignKey('shops.shop_id', ondelete='CASCADE'), nullable=False)
    affiliate_id = Column(BigInteger, nullable=False)
    # Default amount = 1 for testing. For production, change to default = 1300
    amount = Column(Integer, default=1, nullable=False)
    currency = Column(Text, default='XTR', nullable=False)
    status = Column(Text, default='pending', nullable=False)  # 'pending', 'paid', 'validated', 'failed'
    telegram_charge_id = Column(Text, nullable=True)
    paid_at = Column(TIMESTAMP, nullable=True)
    validated_at = Column(TIMESTAMP, nullable=True)
    master_notified = Column(Boolean, default=False, nullable=False)
    shop_activated = Column(Boolean, default=False, nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)
    last_retry_at = Column(TIMESTAMP, nullable=True)
    error_message = Column(Text, nullable=True)
    validation_checksum = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=lambda: datetime.utcnow(), nullable=False)
    
    # Table constraints and indexes
    __table_args__ = (
        # Removed CheckConstraint on amount for scalability - allows flexible payment amounts
        # Validation is handled in Python code (payment_service.py) for easier updates
        Index('idx_affiliate_payments_shop_id', 'shop_id'),
        Index('idx_affiliate_payments_affiliate_id', 'affiliate_id'),
        Index('idx_affiliate_payments_status', 'status'),
        Index('idx_stuck_payments', 'status', 'master_notified', 'created_at', 
              postgresql_where=sql_text("status = 'paid' AND master_notified = FALSE")),
        {'comment': 'Tracks affiliate payments processed by Shop Bots'}
    )


class PaymentAudit(Base):
    """Payment audit log for tracking all payment-related events"""
    __tablename__ = 'payment_audit'
    
    id = Column(BigIntPK, primary_key=True, autoincrement=True)
    invoice_id = Column(String, ForeignKey('affiliate_payments.invoice_id', ondelete='CASCADE'), nullable=False)
    action = Column(Text, nullable=False)  # 'invoice_created', 'payment_received', 'shop_activated', 'credits_deducted', 'error', 'recovered_and_activated'
    shop_id = Column(String, nullable=True)
    affiliate_id = Column(BigInteger, nullable=True)
    amount = Column(Integer, nullable=True)
    credits_deducted = Column(Integer, nullable=True)
    previous_balance = Column(Integer, nullable=True)
    new_balance = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    timestamp = Column(TIMESTAMP, default=lambda: datetime.utcnow(), nullable=False)
    
    __table_args__ = (
        Index('idx_audit_invoice', 'invoice_id'),
        Index('idx_audit_timestamp', 'timestamp'),
        {'comment': 'Audit trail for all affiliate payment operations'}
    )
