"""Display service-level price intervals from Snowflake IN_SERVICE_DISPERSION_SQL."""
import altair as alt
import pandas as pd
import streamlit as st

st.set_page_config(page_title='UHC price dispersion', layout='wide')
st.title('How dispersed are prices within the same service?')
st.caption('OHBS sample · CPT 2026 · professional / outpatient · Office 11 · no modifiers or additional conditions')
connection = st.connection('snowflake')
data = connection.query('''
    SELECT FILE_KEY, SOURCE_FILENAME, SERVICE_POSITION, BILLING_CODE, SERVICE_NAME,
           TO_VARCHAR(EXPIRATION_DATE) AS EXPIRY, RATE_RECORDS, P25_RATE, MEDIAN_RATE, P75_RATE
    FROM UHC_DB.ANALYTICS.IN_SERVICE_DISPERSION_SQL
    ORDER BY MEDIAN_RATE DESC
''', ttl=0)

for column in ('P25_RATE', 'MEDIAN_RATE', 'P75_RATE'):
    data[column] = pd.to_numeric(data[column])

labels = []
for row in data.to_dict('records'):
    labels.append(f"{row['BILLING_CODE']} · {row['SERVICE_NAME']} · {row['SOURCE_FILENAME']} · {row['SERVICE_POSITION']} · {row['EXPIRY']}")
data['SERVICE'] = labels

if data.empty:
    st.info('No services meet the selected conditions and the 20-record / two-amount display threshold.')
else:
    base = alt.Chart(data).encode(
        y=alt.Y('SERVICE:N', title=None, sort=alt.SortField(field='MEDIAN_RATE', order='descending'),
                axis=alt.Axis(labelLimit=360)),
        tooltip=['BILLING_CODE', 'SERVICE_NAME', 'RATE_RECORDS', 'P25_RATE', 'MEDIAN_RATE',
                 'P75_RATE', 'SOURCE_FILENAME', 'SERVICE_POSITION', 'EXPIRY'])
    interval = base.mark_rule(strokeWidth=7, color='#2878a8').encode(
        x=alt.X('P25_RATE:Q', title='Published negotiated amount', scale=alt.Scale(zero=True)),
        x2='P75_RATE:Q')
    median = base.mark_point(filled=True, size=65, color='#e68a3f').encode(x='MEDIAN_RATE:Q')
    st.altair_chart((interval + median).properties(height=max(250, 30 * len(data))), use_container_width=True)

st.caption('Blue line: middle 50% of source prices (P25–P75). Orange point: median. Wider lines mean larger absolute price dispersion.')
st.caption('At least 20 records and two amounts per source service/expiration group. Each source price has equal weight; records are not visits. Differences are investigation leads, not savings estimates.')
