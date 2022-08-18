import './FilterIncident.scss';
import {Checkbox} from "@mui/material";
import {FilterOption} from "./FilterUtils";

type Props = {
    filterIncident: FilterOption
    checked: (e: FilterOption) => void
}

export const FilterIncident = ({filterIncident, checked}: Props) => {

    return (
    <div className='incident'>
        <Checkbox checked={filterIncident.selected} onChange={(e) => checked({
            incident: filterIncident.incident,
            selected: e.target.checked
        })} />
        <div>
            <p>{filterIncident.incident.context}</p>
        </div>
    </div>);
}
