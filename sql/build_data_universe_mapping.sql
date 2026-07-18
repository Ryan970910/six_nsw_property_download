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
        count(DISTINCT propdb_staging.normalise_parcel_address(address))::integer AS address_count,
        min(propdb_staging.normalise_parcel_address(address)) AS canonical_address,
        string_agg(DISTINCT valuation_table, ',' ORDER BY valuation_table) AS valuation_tables,
        max(validity_d) AS latest_valuation_validity_date,
        max(latest_basis_date) AS latest_valuation_basis_date
    FROM land_valuation
    WHERE propid IS NOT NULL
    GROUP BY propid
),
sales_by_prop AS (
    SELECT url_property_id AS propid, max(sale_date) AS latest_sale_date
    FROM propdb_staging.nsw_property_sales_all_history
    WHERE url_property_id IS NOT NULL
    GROUP BY url_property_id
),
property_source_addresses AS (
    SELECT
        propid,
        propdb_staging.normalise_parcel_address(address) AS canonical_address,
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
)
SELECT
    du.prop_id,
    du.address AS original_address,
    COALESCE(
        CASE WHEN lv.address_count = 1 THEN lv.canonical_address END,
        propdb_staging.normalise_parcel_address(du.address),
        CASE WHEN ps.base_address_count = 1 THEN ps.single_base_address END
    ) AS canonical_address,
    CASE
        WHEN lv.address_count = 1 THEN 'land_valuation'
        WHEN propdb_staging.normalise_parcel_address(du.address) IS NOT NULL THEN 'data_universe'
        WHEN ps.base_address_count = 1 THEN 'property_source'
        ELSE 'none'
    END AS canonical_address_source,
    propdb_staging.address_has_unit_number(du.address) AS data_universe_address_has_unit,
    COALESCE(ps.has_unit_addresses, false) AS property_source_has_unit_addresses,
    COALESCE(ps.base_address_count, 0) AS property_source_base_address_count,
    lv.propid IS NOT NULL AS has_land_valuation,
    COALESCE(lv.address_count, 0) AS land_valuation_address_count,
    COALESCE(lv.address_count, 0) > 1 AS land_valuation_address_conflict,
    lv.valuation_tables,
    lv.latest_valuation_basis_date,
    lv.latest_valuation_validity_date,
    sales.latest_sale_date,
    ps.latest_property_update,
    CASE
        WHEN lv.propid IS NOT NULL THEN 'land_valuation'
        WHEN sales.propid IS NOT NULL THEN 'sale_history_only'
        WHEN ps.propid IS NOT NULL THEN 'property_source_only'
        ELSE 'data_universe_only'
    END AS activity_evidence,
    now() AS generated_at
FROM propdb_staging.data_universe du
LEFT JOIN land_valuation_by_prop lv ON lv.propid = du.prop_id
LEFT JOIN sales_by_prop sales ON sales.propid = du.prop_id
LEFT JOIN property_source_by_prop ps ON ps.propid = du.prop_id;

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
            WHERE has_land_valuation AND NOT land_valuation_address_conflict
        )::integer AS land_valuation_prop_id_count,
        min(prop_id) FILTER (
            WHERE has_land_valuation AND NOT land_valuation_address_conflict
        ) AS unique_land_valuation_prop_id
    FROM propdb_staging.data_universe_prop_id_candidates
    WHERE canonical_address IS NOT NULL
    GROUP BY canonical_address
)
SELECT
    c.prop_id,
    c.canonical_address,
    CASE
        WHEN c.canonical_address IS NULL THEN NULL
        WHEN c.land_valuation_address_conflict THEN NULL
        WHEN g.land_valuation_prop_id_count = 1 AND c.prop_id = g.unique_land_valuation_prop_id THEN c.prop_id
        WHEN g.land_valuation_prop_id_count = 1
             AND c.latest_sale_date IS NOT NULL
             AND (
                 primary_candidate.latest_valuation_basis_date IS NULL
                 OR c.latest_sale_date >= primary_candidate.latest_valuation_basis_date
             ) THEN NULL
        WHEN g.land_valuation_prop_id_count = 1 THEN g.unique_land_valuation_prop_id
        WHEN g.land_valuation_prop_id_count = 0 AND g.prop_id_count = 1 THEN c.prop_id
        ELSE NULL
    END AS primary_prop_id,
    CASE
        WHEN c.canonical_address IS NULL THEN 'blank_address'
        WHEN c.land_valuation_address_conflict THEN 'review_land_valuation_address_conflict'
        WHEN g.land_valuation_prop_id_count = 1 AND c.prop_id = g.unique_land_valuation_prop_id THEN 'primary'
        WHEN g.land_valuation_prop_id_count = 1
             AND c.latest_sale_date IS NOT NULL
             AND (
                 primary_candidate.latest_valuation_basis_date IS NULL
                 OR c.latest_sale_date >= primary_candidate.latest_valuation_basis_date
             ) THEN 'review_recent_transaction_after_valuation'
        WHEN g.land_valuation_prop_id_count = 1 THEN 'obsolete_candidate'
        WHEN g.land_valuation_prop_id_count > 1 THEN 'review_multiple_land_valuation_prop_ids'
        WHEN g.prop_id_count = 1 THEN 'primary_unverified'
        ELSE 'review_no_land_valuation_primary'
    END AS mapping_status,
    CASE
        WHEN g.land_valuation_prop_id_count = 1
             AND c.prop_id <> g.unique_land_valuation_prop_id
             AND c.latest_sale_date IS NOT NULL
             AND (
                 primary_candidate.latest_valuation_basis_date IS NULL
                 OR c.latest_sale_date >= primary_candidate.latest_valuation_basis_date
             ) THEN 'review'
        WHEN g.land_valuation_prop_id_count = 1 THEN 'high'
        WHEN g.land_valuation_prop_id_count = 0 AND g.prop_id_count = 1 THEN 'medium'
        ELSE 'review'
    END AS mapping_confidence,
    g.land_valuation_prop_id_count = 1
        AND c.prop_id <> g.unique_land_valuation_prop_id
        AND c.latest_sale_date IS NOT NULL
        AND (
            primary_candidate.latest_valuation_basis_date IS NULL
            OR c.latest_sale_date >= primary_candidate.latest_valuation_basis_date
        ) AS recent_transaction_conflict,
    COALESCE(g.prop_id_count, 1) AS prop_ids_at_address,
    COALESCE(g.land_valuation_prop_id_count, 0) AS land_valuation_prop_ids_at_address,
    c.latest_valuation_basis_date,
    c.latest_valuation_validity_date,
    c.latest_sale_date,
    c.activity_evidence,
    c.generated_at
FROM propdb_staging.data_universe_prop_id_candidates c
LEFT JOIN address_groups g ON g.canonical_address = c.canonical_address
LEFT JOIN propdb_staging.data_universe_prop_id_candidates primary_candidate
    ON primary_candidate.prop_id = g.unique_land_valuation_prop_id;

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
WHERE mapping_status IN (
    'blank_address',
    'review_land_valuation_address_conflict',
    'review_multiple_land_valuation_prop_ids',
    'review_no_land_valuation_primary',
    'review_recent_transaction_after_valuation'
);
