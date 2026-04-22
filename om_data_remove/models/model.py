import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    def _execute_sql(self, query, params=None):
        """Helper to execute SQL safely."""
        try:
            self.env.cr.execute(query, params)
            self.env.cr.commit()  # Force commit to ensure deletion
        except Exception as e:
            _logger.error("SQL Error: %s -> %s", query, e)
            self.env.cr.rollback()

    def _truncate_table(self, table_name):
        """Truncate table if possible, otherwise delete."""
        try:
            # Check if table exists
            self.env.cr.execute("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = %s);", (table_name,))
            if not self.env.cr.fetchone()[0]:
                return
            
            # Try TRUNCATE CASCADE (Fastest, removes all data and resets IDs)
            # Note: TRUNCATE cannot be used if there are active transactions referencing it in some PG configs, 
            # but usually works in Odoo context if we commit frequently.
            self.env.cr.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY CASCADE;")
            self.env.cr.commit()
        except Exception as e:
            _logger.warning("Truncate failed for %s, falling back to DELETE. Error: %s", table_name, e)
            try:
                self.env.cr.execute(f"DELETE FROM {table_name};")
                self.env.cr.commit()
            except Exception as e2:
                _logger.error("Delete failed for %s: %s", table_name, e2)

    def _reset_sequences(self, prefixes):
        """Reset sequences based on prefixes."""
        for prefix in prefixes:
            domain = [
                '|', ('code', '=ilike', f'{prefix}%'),
                ('prefix', '=ilike', f'{prefix}%')
            ]
            seqs = self.env['ir.sequence'].sudo().search(domain)
            if seqs:
                seqs.write({'number_next': 1})
                _logger.info("Reset sequences for prefix: %s", prefix)

    def remove_sales(self):
        tables = ['sale_order_line', 'sale_order']
        for t in tables:
            self._truncate_table(t)
        self._reset_sequences(['sale'])
        return True

    def remove_product(self):
        # Must delete product.product before product.template
        tables = ['product_product', 'product_template']
        for t in tables:
            self._truncate_table(t)
        self._reset_sequences(['product.product'])
        return True

    def remove_product_attribute(self):
        tables = ['product_attribute_value', 'product_attribute']
        for t in tables:
            self._truncate_table(t)
        return True

    def remove_pos(self):
        tables = [
            'pos_payment',
            'pos_order_line',
            'pos_order',
            'pos_session',
        ]
        for t in tables:
            self._truncate_table(t)
        self._reset_sequences(['pos'])
        
        # Clean up related bank statements if they were created by POS
        try:
            self.env.cr.execute("DELETE FROM account_bank_statement_line WHERE pos_session_id IS NOT NULL;")
            self.env.cr.commit()
        except:
            pass
        return True

    def remove_purchase(self):
        tables = [
            'purchase_order_line',
            'purchase_order',
            'purchase_requisition_line',
            'purchase_requisition',
        ]
        for t in tables:
            self._truncate_table(t)
        self._reset_sequences(['purchase'])
        return True

    def remove_expense(self):
        tables = [
            'hr_expense_sheet',
            'hr_expense',
            'hr_payslip',
            'hr_payslip_run',
        ]
        for t in tables:
            self._truncate_table(t)
        self._reset_sequences(['hr.expense'])
        return True

    def remove_mrp(self):
        tables = [
            'mrp_workcenter_productivity',
            'mrp_workorder',
            'mrp_production_workcenter_line',
            'change_production_qty',
            'mrp_production',
            'mrp_production_product_line',
            'mrp_unbuild',
        ]
        for t in tables:
            self._truncate_table(t)
        self._reset_sequences(['mrp'])
        return True

    def remove_mrp_bom(self):
        tables = ['mrp_bom_line', 'mrp_bom']
        for t in tables:
            self._truncate_table(t)
        return True

    def remove_inventory(self):
        # Strict order: Quant/MoveLine -> Move -> Picking -> Lot -> Package
        tables = [
            'stock_quant',
            'stock_move_line',
            'stock_package_level',
            'stock_quantity_history',
            'stock_valuation_layer', # Important for accounting consistency
            'stock_move',
            'stock_picking',
            'stock_scrap',
            'stock_picking_batch',
            'stock_inventory_line',
            'stock_inventory',
            'stock_lot', # Odoo 17 uses stock_lot
            'stock_production_lot', # Legacy
            'procurement_group',
        ]
        for t in tables:
            self._truncate_table(t)
        self._reset_sequences(['stock', 'picking', 'WH/'])
        return True

    def remove_account(self):
        # Accounting is the most dangerous. Order: Payments/Lines -> Moves -> Journals/Taxes
        tables = [
            'payment_transaction',
            'account_bank_statement_line',
            'account_payment',
            'account_analytic_line',
            'account_partial_reconcile',
            'account_move_line',
            'account_move',
            'account_bank_statement',
        ]
        for t in tables:
            self._truncate_table(t)
        
        # Reset Sequences
        company_id = self.env.company.id
        self.env.cr.execute("""
            UPDATE ir_sequence SET number_next = 1 
            WHERE company_id = %s AND (
                code LIKE 'account.%%' OR 
                prefix LIKE 'BNK1/%%' OR 
                prefix LIKE 'CSH1/%%' OR 
                prefix LIKE 'INV/%%' OR 
                prefix LIKE 'EXCH/%%' OR 
                prefix LIKE 'MISC/%%'
            );
        """, (company_id,))
        self.env.cr.commit()
        return True

    def remove_account_chart(self):
        """Completely wipes accounting configuration. Use with extreme caution."""
        company_id = self.env.company.id
        
        # 1. Clear Properties (IrValues/Defaults)
        self.env.cr.execute("""
            DELETE FROM ir_default 
            WHERE field_id IN (
                SELECT id FROM ir_model_fields 
                WHERE model = 'product.template' AND name IN ('taxes_id', 'supplier_taxes_id')
            ) AND company_id = %s;
        """, (company_id,))
        
        # 2. Clear Journal Bank Links
        self.env.cr.execute("UPDATE account_journal SET bank_account_id = NULL WHERE company_id = %s;", (company_id,))
        
        # 3. Clear Partner Properties
        self.env.cr.execute("""
            UPDATE res_partner 
            SET property_account_receivable_id = NULL, 
                property_account_payable_id = NULL 
            WHERE company_id = %s OR company_id IS NULL;
        """, (company_id,))

        # 4. Clear Category/Product Properties
        self.env.cr.execute("""
            UPDATE product_category 
            SET property_account_income_categ_id = NULL, 
                property_account_expense_categ_id = NULL,
                property_stock_account_input_categ_id = NULL,
                property_stock_account_output_categ_id = NULL,
                property_stock_valuation_account_id = NULL;
        """)
        
        self.env.cr.execute("""
            UPDATE product_template 
            SET property_account_income_id = NULL, 
                property_account_expense_id = NULL;
        """)

        self.env.cr.commit()

        # 5. Delete Accounting Config Tables
        tables = [
            'res_partner_bank',
            'account_tax',
            'account_tax_account_tag',
            'account_account_tag',
            'account_journal',
            'account_account',
        ]
        for t in tables:
            self._truncate_table(t)
            
        return True

    def remove_project(self):
        tables = [
            'account_analytic_line',
            'project_task',
            'project_forecast',
            'project_project',
        ]
        for t in tables:
            self._truncate_table(t)
        return True

    def remove_quality(self):
        tables = ['quality_check', 'quality_alert']
        for t in tables:
            self._truncate_table(t)
        self._reset_sequences(['quality'])
        return True

    def remove_quality_setting(self):
        tables = [
            'quality_point',
            'quality_alert_stage',
            'quality_alert_team',
            'quality_point_test_type',
            'quality_reason',
            'quality_tag',
        ]
        for t in tables:
            self._truncate_table(t)
        return True

    def remove_website(self):
        tables = [
            'blog_tag_category',
            'blog_tag',
            'blog_post',
            'blog_blog',
            'product_wishlist',
            'website_visitor',
            'website_redirect',
            'website_seo_metadata',
        ]
        for t in tables:
            self._truncate_table(t)
        return True

    def remove_message(self):
        # Messages are linked to many things. Truncating them is safe if you don't need chat history.
        tables = ['mail_message', 'mail_followers', 'mail_activity']
        for t in tables:
            self._truncate_table(t)
        return True

    def remove_all(self):
        """Executes all removal methods in the correct dependency order."""
        _logger.warning("STARTING FULL DATABASE CLEANUP. THIS IS IRREVERSIBLE.")
        
        # 1. Remove Dependencies (Messages, Logs)
        self.remove_message()
        
        # 2. Remove Operational Data (Sales, Purchases, etc.)
        self.remove_pos()
        self.remove_sales()
        self.remove_purchase()
        self.remove_expense()
        self.remove_project()
        
        # 3. Remove Inventory & MRP (Depends on Products)
        self.remove_mrp()
        self.remove_mrp_bom()
        self.remove_inventory()
        
        # 4. Remove Quality & Website
        self.remove_quality()
        self.remove_quality_setting()
        self.remove_website()
        
        # 5. Remove Products (Now that no moves/orders reference them)
        self.remove_product()
        self.remove_product_attribute()
        
        # 6. Remove Accounting (Last, as it depends on everything else)
        self.remove_account()
        self.remove_account_chart()
        
        # 7. Cleanup
        self.reset_cat_loc_name()
        
        _logger.warning("DATABASE CLEANUP COMPLETED.")
        return True

    def reset_cat_loc_name(self):
        """Recompute names for categories and locations."""
        try:
            self.env.cr.execute("""
                UPDATE product_category 
                SET complete_name = name 
                WHERE parent_id IS NULL;
                
                UPDATE stock_location 
                SET complete_name = name 
                WHERE location_id IS NULL OR usage = 'view';
            """)
            self.env.cr.commit()
        except Exception as e:
            _logger.error("Error resetting names: %s", e)
        return True