CREATE OR REPLACE FUNCTION propdb_staging.normalise_parcel_address(value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    result text;
    replacement text[];
BEGIN
    IF value IS NULL THEN
        RETURN NULL;
    END IF;

    result := regexp_replace(value, '^[[:space:]]*[A-Za-z0-9-]+[[:space:]]*/[[:space:]]*', '');
    result := regexp_replace(result, ',?[[:space:]]+NSW[[:space:]]+[0-9]{4}[[:space:]]*$', '', 'i');
    result := regexp_replace(upper(btrim(result)), '[,.]', ' ', 'g');

    FOREACH replacement SLICE 1 IN ARRAY ARRAY[
        ['ST', 'STREET'], ['RD', 'ROAD'], ['AV', 'AVENUE'], ['AVE', 'AVENUE'],
        ['DR', 'DRIVE'], ['CCT', 'CIRCUIT'], ['CIR', 'CIRCUIT'], ['PL', 'PLACE'],
        ['CT', 'COURT'], ['CRT', 'COURT'], ['CRES', 'CRESCENT'], ['PDE', 'PARADE'],
        ['HWY', 'HIGHWAY'], ['LN', 'LANE'], ['CL', 'CLOSE'], ['TCE', 'TERRACE']
    ]::text[][] LOOP
        result := regexp_replace(
            result,
            '(^|[[:space:]])' || replacement[1] || '([[:space:]]|$)',
            '\1' || replacement[2] || '\2',
            'g'
        );
    END LOOP;

    RETURN NULLIF(regexp_replace(btrim(result), '[[:space:]]+', ' ', 'g'), '');
END;
$$;

CREATE OR REPLACE FUNCTION propdb_staging.address_has_unit_number(value text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT COALESCE(value ~ '^[[:space:]]*[A-Za-z0-9-]+[[:space:]]*/', false)
$$;

CREATE TABLE propdb_staging.data_universe_prop_id_candidates AS
WITH land_valuation AS (
    SELECT 'urban'::text AS valuation_table, propid, address, validity_d,
           greatest(val1_bd, val2_bd, val3_bd, val4_bd, val5_bd) AS latest_basis_date
    FROM propdb_staging.nsw_land_valuation_urban
    UNION ALL
    SELECT 'semi', propid, address, validity_d,
           greatest(val1_bd, val2_bd, val3_bd, val4_bd, val5_bd)
    FROM propdb_staging.nsw_land_valuation_semi
    UNION ALL
    SELECT 'rural', propid, address, validity_d,
           greatest(val1_bd, val2_bd, val3_bd, val4_bd, val5_bd)
    FROM propdb_staging.nsw_land_valuation_rural
),
land_valuation_by_prop AS (
    SELECT
        propid,
        count(DISTINCT propdb_staging.normalise_parcel_address(address)) FILTER (
            WHERE NOT propdb_staging.address_has_unit_number(address)
        )::integer AS address_count,
        min(propdb_staging.normalise_parcel_address(address)) FILTER (
            WHERE NOT propdb_staging.address_has_unit_number(address)
        ) AS canonical_address,
        bool_or(propdb_staging.address_has_unit_number(address)) AS has_unit_addresses,
        string_agg(DISTINCT valuation_table, ',' ORDER BY valuation_table) AS valuation_tables,
        max(validity_d) AS latest_valuation_validity_date,
        max(latest_basis_date) AS latest_valuation_basis_date
    FROM land_valuation
    WHERE propid IS NOT NULL
    GROUP BY propid
),
sales_by_prop AS (
    SELECT
        url_property_id AS propid,
        max(sale_date) AS latest_sale_date,
        max(imported_at) FILTER (
            WHERE imported_at >= timestamptz '2026-06-01 00:00:00+00'
        ) AS latest_recent_import_at
    FROM propdb_staging.nsw_property_sales_all_history
    WHERE url_property_id IS NOT NULL
    GROUP BY url_property_id
),
property_source_addresses AS (
    SELECT
        propid,
        CASE
            WHEN NOT propdb_staging.address_has_unit_number(address)
            THEN propdb_staging.normalise_parcel_address(address)
        END AS canonical_address,
        propdb_staging.address_has_unit_number(address) AS has_unit_address,
        lastupdate
    FROM propdb_staging.nsw_property_source
    WHERE propid IS NOT NULL
),
property_source_by_prop AS (
    SELECT
        propid,
        count(DISTINCT canonical_address) FILTER (WHERE canonical_address IS NOT NULL)::integer AS base_address_count,
        min(canonical_address) FILTER (WHERE canonical_address IS NOT NULL) AS single_base_address,
        bool_or(has_unit_address) AS has_unit_addresses,
        to_timestamp(max(lastupdate)::double precision / 1000.0) AS latest_property_update
    FROM property_source_addresses
    GROUP BY propid
),
candidate_base AS (
SELECT
    du.prop_id,
    du.address AS original_address,
    COALESCE(
        CASE WHEN lv.address_count = 1 THEN lv.canonical_address END,
        CASE
            WHEN NOT propdb_staging.address_has_unit_number(du.address)
            THEN propdb_staging.normalise_parcel_address(du.address)
        END,
        CASE WHEN ps.base_address_count = 1 THEN ps.single_base_address END
    ) AS canonical_address,
    CASE
        WHEN lv.address_count = 1 THEN 'land_valuation'
        WHEN NOT propdb_staging.address_has_unit_number(du.address)
             AND propdb_staging.normalise_parcel_address(du.address) IS NOT NULL
        THEN 'data_universe'
        WHEN ps.base_address_count = 1 THEN 'property_source'
        ELSE 'none'
    END AS canonical_address_source,
    propdb_staging.address_has_unit_number(du.address) AS data_universe_address_has_unit,
    COALESCE(ps.has_unit_addresses, false) AS property_source_has_unit_addresses,
    COALESCE(ps.base_address_count, 0) AS property_source_base_address_count,
    lv.propid IS NOT NULL AS has_land_valuation,
    COALESCE(lv.address_count, 0) AS land_valuation_address_count,
    COALESCE(lv.address_count, 0) > 1 AS land_valuation_address_conflict,
    COALESCE(lv.has_unit_addresses, false) AS land_valuation_has_unit_addresses,
    lv.valuation_tables,
    lv.latest_valuation_basis_date,
    lv.latest_valuation_validity_date,
    sales.propid IS NOT NULL AS has_historical_sale,
    sales.latest_recent_import_at IS NOT NULL AS has_recent_historical_import,
    sales.latest_recent_import_at,
    sales.latest_sale_date,
    ps.latest_property_update,
    CASE
        WHEN lv.propid IS NOT NULL THEN 'land_valuation'
        WHEN sales.propid IS NOT NULL THEN 'sale_history_only'
        WHEN ps.propid IS NOT NULL THEN 'property_source_only'
        ELSE 'data_universe_only'
    END AS activity_evidence
FROM propdb_staging.data_universe du
LEFT JOIN land_valuation_by_prop lv ON lv.propid = du.prop_id
LEFT JOIN sales_by_prop sales ON sales.propid = du.prop_id
LEFT JOIN property_source_by_prop ps ON ps.propid = du.prop_id
)
SELECT
    candidate_base.*,
    canonical_address IS NOT NULL AND NOT land_valuation_address_conflict
        AS eligible_for_primary_address,
    now() AS generated_at
FROM candidate_base;

ALTER TABLE propdb_staging.data_universe_prop_id_candidates
    ADD CONSTRAINT data_universe_prop_id_candidates_pkey PRIMARY KEY (prop_id);
CREATE INDEX idx_data_universe_candidates_address
    ON propdb_staging.data_universe_prop_id_candidates (canonical_address);
CREATE INDEX idx_data_universe_candidates_latest_sale
    ON propdb_staging.data_universe_prop_id_candidates (latest_sale_date DESC);

CREATE TABLE propdb_staging.data_universe_master_map AS
WITH address_groups AS (
    SELECT
        canonical_address,
        count(*)::integer AS prop_id_count,
        count(*) FILTER (
            WHERE has_land_valuation AND has_historical_sale
        )::integer AS sales_and_land_valuation_prop_id_count,
        min(prop_id) FILTER (
            WHERE has_land_valuation AND has_historical_sale
        ) AS unique_sales_and_land_valuation_prop_id,
        count(*) FILTER (
            WHERE has_land_valuation
        )::integer AS land_valuation_prop_id_count,
        min(prop_id) FILTER (
            WHERE has_land_valuation
        ) AS unique_land_valuation_prop_id
    FROM propdb_staging.data_universe_prop_id_candidates
    WHERE eligible_for_primary_address
    GROUP BY canonical_address
),
step4_max_dates AS (
    SELECT
        c.canonical_address,
        count(*)::integer AS recent_historical_sale_prop_id_count,
        max(c.latest_sale_date) AS latest_recent_sale_date
    FROM propdb_staging.data_universe_prop_id_candidates c
    JOIN address_groups g USING (canonical_address)
    WHERE c.eligible_for_primary_address
      AND c.has_recent_historical_import
      AND g.prop_id_count > 1
      AND g.sales_and_land_valuation_prop_id_count <> 1
      AND g.land_valuation_prop_id_count <> 1
    GROUP BY c.canonical_address
),
step4_choices AS (
    SELECT
        s.canonical_address,
        s.recent_historical_sale_prop_id_count,
        s.latest_recent_sale_date,
        count(*) FILTER (
            WHERE c.latest_sale_date = s.latest_recent_sale_date
        )::integer AS latest_sale_date_prop_id_count,
        min(c.prop_id) FILTER (
            WHERE c.latest_sale_date = s.latest_recent_sale_date
        ) AS unique_latest_sale_prop_id
    FROM step4_max_dates s
    JOIN propdb_staging.data_universe_prop_id_candidates c
        ON c.canonical_address = s.canonical_address
       AND c.eligible_for_primary_address
       AND c.has_recent_historical_import
    GROUP BY
        s.canonical_address,
        s.recent_historical_sale_prop_id_count,
        s.latest_recent_sale_date
),
address_decisions AS (
    SELECT
        g.*,
        COALESCE(s.recent_historical_sale_prop_id_count, 0)
            AS recent_historical_sale_prop_id_count,
        COALESCE(s.latest_sale_date_prop_id_count, 0)
            AS latest_sale_date_prop_id_count,
        s.latest_recent_sale_date,
        CASE
            WHEN g.sales_and_land_valuation_prop_id_count = 1
            THEN g.unique_sales_and_land_valuation_prop_id
            WHEN g.land_valuation_prop_id_count = 1
            THEN g.unique_land_valuation_prop_id
            WHEN s.latest_sale_date_prop_id_count = 1
            THEN s.unique_latest_sale_prop_id
        END AS selected_primary_prop_id,
        CASE
            WHEN g.sales_and_land_valuation_prop_id_count = 1
            THEN 'historical_sales_and_land_valuation'
            WHEN g.land_valuation_prop_id_count = 1
            THEN 'land_valuation_only'
            WHEN s.latest_sale_date_prop_id_count = 1
            THEN 'recent_historical_sales_latest_sale'
        END AS primary_selection_rule
    FROM address_groups g
    LEFT JOIN step4_choices s USING (canonical_address)
)
SELECT
    c.prop_id,
    c.canonical_address,
    CASE
        WHEN c.land_valuation_address_conflict THEN NULL
        ELSE d.selected_primary_prop_id
    END AS primary_prop_id,
    d.primary_selection_rule,
    CASE
        WHEN c.canonical_address IS NULL AND c.data_universe_address_has_unit
        THEN 'ignored_unit_address'
        WHEN c.canonical_address IS NULL THEN 'blank_address'
        WHEN c.land_valuation_address_conflict THEN 'review_land_valuation_address_conflict'
        WHEN d.selected_primary_prop_id = c.prop_id
             AND d.primary_selection_rule = 'historical_sales_and_land_valuation'
        THEN 'primary_sales_and_land_valuation'
        WHEN d.selected_primary_prop_id = c.prop_id
             AND d.primary_selection_rule = 'land_valuation_only'
        THEN 'primary_land_valuation_only'
        WHEN d.selected_primary_prop_id = c.prop_id
             AND d.primary_selection_rule = 'recent_historical_sales_latest_sale'
        THEN 'primary_latest_recent_sale'
        WHEN d.selected_primary_prop_id IS NOT NULL THEN 'obsolete_candidate'
        WHEN d.latest_sale_date_prop_id_count > 1 THEN 'review_latest_sale_date_tie'
        WHEN d.sales_and_land_valuation_prop_id_count > 1
        THEN 'review_multiple_sales_and_land_valuation_prop_ids'
        WHEN d.land_valuation_prop_id_count > 1
        THEN 'review_multiple_land_valuation_prop_ids'
        ELSE 'review_no_primary_evidence'
    END AS mapping_status,
    CASE
        WHEN d.primary_selection_rule = 'historical_sales_and_land_valuation' THEN 'high'
        WHEN d.primary_selection_rule IN (
            'land_valuation_only',
            'recent_historical_sales_latest_sale'
        ) THEN 'medium'
        ELSE 'review'
    END AS mapping_confidence,
    COALESCE(d.prop_id_count, 0) AS prop_ids_at_address,
    COALESCE(d.sales_and_land_valuation_prop_id_count, 0)
        AS sales_and_land_valuation_prop_ids_at_address,
    COALESCE(d.land_valuation_prop_id_count, 0) AS land_valuation_prop_ids_at_address,
    COALESCE(d.recent_historical_sale_prop_id_count, 0)
        AS recent_historical_sale_prop_ids_at_address,
    d.latest_recent_sale_date AS latest_recent_sale_date_at_address,
    c.has_historical_sale,
    c.has_recent_historical_import,
    c.latest_recent_import_at,
    c.latest_valuation_basis_date,
    c.latest_valuation_validity_date,
    c.latest_sale_date,
    c.activity_evidence,
    c.generated_at
FROM propdb_staging.data_universe_prop_id_candidates c
LEFT JOIN address_decisions d ON d.canonical_address = c.canonical_address;

ALTER TABLE propdb_staging.data_universe_master_map
    ADD CONSTRAINT data_universe_master_map_pkey PRIMARY KEY (prop_id);
CREATE INDEX idx_data_universe_master_primary
    ON propdb_staging.data_universe_master_map (primary_prop_id);
CREATE INDEX idx_data_universe_master_address
    ON propdb_staging.data_universe_master_map (canonical_address);
CREATE INDEX idx_data_universe_master_status
    ON propdb_staging.data_universe_master_map (mapping_status);

CREATE VIEW propdb_staging.data_universe_mapping_review AS
SELECT *
FROM propdb_staging.data_universe_master_map
WHERE primary_prop_id IS NULL;
