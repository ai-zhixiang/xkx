    # 时间
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 虾点资源体系
    phone = Column(String(32), unique=True, nullable=True)
    xiake_points = Column(Integer, nullable=False, default=3000)
    points_expires_at = Column(Date, nullable=True)
    total_points_consumed = Column(Integer, nullable=False, default=0)
    total_points_recharged = Column(Integer, nullable=False, default=0)
    disk_quota_bytes = Column(BigInteger, nullable=False, default=2147483648)
    disk_used_bytes = Column(BigInteger, nullable=False, default=0)

    conversations = relationship('ChatConversation', back_populates='subscriber')
    orders = relationship('SubOrder', back_populates='subscriber')