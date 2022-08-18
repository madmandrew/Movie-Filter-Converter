import React, {useState} from "react";
import './FilterConverter.scss';
import ReactJson from 'react-json-view'
import {Filter, FilterSettings} from "./FilterTypes";
import {
    FilterOption,
    formatFilterSettings,
    ClearplayFilterGroup,
    convertToVideoSkip,
    convertToEDLFormat
} from "./FilterUtils";
import {FilterIncident} from "./FilterIncident";
import {Accordion, AccordionDetails, AccordionSummary, Button, TextField} from "@mui/material";
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';

const jsonViewerStyle: any = {
    backgroundColor: 'white',
    width: '500px',
    height: '500px',
    overflow: 'auto',
    textAlign: 'left'
}

export default function FilterConverter() {
    const [filterSettings, setFilterSettings] = useState<FilterSettings>({} as any)
    const [filters, setFilter] = useState<Filter>({} as any)
    const [videoSkipFilter, setVideoSkipFilter] = useState<string>('')
    const [edlFilter, setEdlFilter] = useState<string>('')
    const [offset, setOffset] = useState<number>(0);
    const [formattedFilters, setFormattedFilters] = useState<ClearplayFilterGroup[]>([])

    const handleFilterSettingsChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
        const newFilterSettings = JSON.parse(e.target.value)
        setFilterSettings(newFilterSettings)

        setFormattedFilters(formatFilterSettings(newFilterSettings))
    }

    const handleFilterChecked = (e: FilterOption) => {
        setFormattedFilters(formattedFilters.map(category => ({
            category: category.category,
            filters: category.filters.map(filter => filter.incident.id === e.incident.id ? e : filter)
        })))
    }

    const handleConvert = () => {
        setVideoSkipFilter(convertToVideoSkip(formattedFilters, filters, offset))
        setEdlFilter(convertToEDLFormat(formattedFilters, filters, offset))
    }

    const handleSelectAll = (filterGroup: ClearplayFilterGroup, value: boolean) => {
        setFormattedFilters(formattedFilters.map(category => ({
            category: category.category,
            filters:  category.category.id === filterGroup.category.id ? filterGroup.filters.map(filter => ({selected: value, incident: filter.incident})) : category.filters
        })))
    }

    return (
        <div style={{display: 'flex', flexDirection: 'column'}}>
            <div className='container'>
                <div className='container-column'>
                    <TextField multiline variant='filled' label='Filter SettingUI' onChange={handleFilterSettingsChange} rows={4} maxRows={4}/>
                    <ReactJson src={filterSettings} style={jsonViewerStyle} />
                </div>
                <div className='container-column'>
                    <TextField multiline variant='filled' label='Filter' onChange={(e) => setFilter(JSON.parse(e.target.value))} rows={4} maxRows={4}/>
                    <ReactJson src={filters} style={jsonViewerStyle} />
                </div>
                <div className='container-column' style={{minWidth: '500px'}}>
                    {formattedFilters.map(filterGroup => (
                    <Accordion>
                        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                            {filterGroup.category.desc} | {filterGroup.filters.length}
                        </AccordionSummary>
                        <AccordionDetails>
                            <Button onClick={() => handleSelectAll(filterGroup, false)}>Deselect All</Button> <Button onClick={() => handleSelectAll(filterGroup, true)}>Select All</Button>
                            {filterGroup.filters.map(incident => (<FilterIncident filterIncident={incident} checked={handleFilterChecked} />))}
                        </AccordionDetails>
                    </Accordion>))}

                    {/*{formattedFilters.map(incident => (<FilterIncident incident={incident} />))}*/}
                </div>
            </div>

            <TextField variant='filled' label='Offset in seconds' onChange={(e) => setOffset(Number(e.target.value))} />

            <Button variant='contained' onClick={handleConvert}>Convert</Button>

            <div style={{display: 'flex', flexDirection: 'row', width: '100%'}}>
                <TextField style={{flexGrow: '1', margin: '.5rem'}} multiline variant='filled' label='Video Skip' value={videoSkipFilter} rows={4} maxRows={4}/>
                <TextField style={{flexGrow: '1', margin: '.5rem'}} multiline variant='filled' label='EDL' value={edlFilter} rows={4} maxRows={4}/>
            </div>
        </div>
    )
}
