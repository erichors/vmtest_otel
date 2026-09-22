package com.aheaddemo.checkout;

import com.aheaddemo.checkout.Dtos.CustomerRow;
import com.aheaddemo.checkout.Dtos.OrderEvent;
import com.aheaddemo.checkout.Dtos.OrderResponse;
import com.aheaddemo.checkout.Dtos.RevenueRow;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.jdbc.support.KeyHolder;
import org.springframework.stereotype.Repository;

import java.math.BigDecimal;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;

/**
 * Plain JdbcTemplate DAO. Every query is parameterized.
 *
 * GET /api/reports/revenue backs onto {@link #revenueByCategory()}, a deliberately
 * heavier join + group-by query so Dynatrace has a visibly slower database span to
 * show in the trace waterfall for that endpoint.
 */
@Repository
public class OrderRepository {

    private final JdbcTemplate jdbc;

    public OrderRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public Optional<CustomerRow> findCustomer(int customerId) {
        try {
            CustomerRow row = jdbc.queryForObject(
                    "SELECT id, name, tier FROM customers WHERE id = ?",
                    (rs, rowNum) -> new CustomerRow(rs.getInt("id"), rs.getString("name"), rs.getString("tier")),
                    customerId);
            return Optional.ofNullable(row);
        } catch (EmptyResultDataAccessException e) {
            return Optional.empty();
        }
    }

    /** Used only by the "dbslow" fault mode to inject a slow, easy-to-spot DB span. */
    public void pgSleep(double seconds) {
        jdbc.query("SELECT pg_sleep(?)", rs -> { /* no rows to consume */ }, seconds);
    }

    public long insertOrder(int customerId, String sku, int qty, BigDecimal unitPrice, BigDecimal totalPrice, String status) {
        KeyHolder keyHolder = new GeneratedKeyHolder();
        jdbc.update(con -> {
            PreparedStatement ps = con.prepareStatement(
                    "INSERT INTO orders (customer_id, sku, qty, unit_price, total_price, status) VALUES (?,?,?,?,?,?)",
                    Statement.RETURN_GENERATED_KEYS);
            ps.setInt(1, customerId);
            ps.setString(2, sku);
            ps.setInt(3, qty);
            ps.setBigDecimal(4, unitPrice);
            ps.setBigDecimal(5, totalPrice);
            ps.setString(6, status);
            return ps;
        }, keyHolder);
        return keyHolder.getKey().longValue();
    }

    public void insertOrderEvent(long orderId, String eventType, String detail) {
        jdbc.update("INSERT INTO order_events (order_id, event_type, detail) VALUES (?,?,?)",
                orderId, eventType, detail);
    }

    public Optional<OrderResponse> findOrderById(long id) {
        List<OrderResponse> rows = jdbc.query(
                "SELECT o.id, o.customer_id, c.name AS customer_name, o.sku, o.qty, o.unit_price, o.total_price, o.status, o.created_at " +
                        "FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.id = ?",
                (rs, rowNum) -> mapOrder(rs), id);
        return rows.stream().findFirst();
    }

    public List<OrderEvent> findEventsByOrderId(long orderId) {
        return jdbc.query(
                "SELECT id, order_id, event_type, detail, created_at FROM order_events WHERE order_id = ? ORDER BY created_at",
                (rs, rowNum) -> new OrderEvent(
                        rs.getLong("id"),
                        rs.getLong("order_id"),
                        rs.getString("event_type"),
                        rs.getString("detail"),
                        rs.getObject("created_at", OffsetDateTime.class)),
                orderId);
    }

    public List<OrderResponse> findRecentOrders(int limit) {
        return jdbc.query(
                "SELECT o.id, o.customer_id, c.name AS customer_name, o.sku, o.qty, o.unit_price, o.total_price, o.status, o.created_at " +
                        "FROM orders o JOIN customers c ON c.id = o.customer_id " +
                        "ORDER BY o.created_at DESC LIMIT ?",
                (rs, rowNum) -> mapOrder(rs), limit);
    }

    /** Deliberately heavier reporting query: full join + group + order by aggregate. */
    public List<RevenueRow> revenueByCategory() {
        return jdbc.query(
                "SELECT p.category AS category, SUM(o.total_price) AS revenue, COUNT(*) AS order_count " +
                        "FROM orders o " +
                        "JOIN products p ON p.sku = o.sku " +
                        "GROUP BY p.category " +
                        "ORDER BY revenue DESC",
                (rs, rowNum) -> new RevenueRow(
                        rs.getString("category"),
                        rs.getBigDecimal("revenue"),
                        rs.getLong("order_count")));
    }

    private OrderResponse mapOrder(ResultSet rs) throws SQLException {
        return new OrderResponse(
                rs.getLong("id"),
                rs.getInt("customer_id"),
                rs.getString("customer_name"),
                rs.getString("sku"),
                rs.getInt("qty"),
                rs.getBigDecimal("unit_price"),
                rs.getBigDecimal("total_price"),
                rs.getString("status"),
                rs.getObject("created_at", OffsetDateTime.class));
    }
}
